import asyncio
import json
import os
import re
import threading
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI
from dotenv import load_dotenv
from flask import Flask

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# --- СОЗДАЁМ FLASK-ПРИЛОЖЕНИЕ (РЕШАЕТ ПРОБЛЕМУ С ПОРТОМ) ---
flask_app = Flask('')

@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return "Bot is running", 200

def run_flask():
    """Запускает Flask-сервер в отдельном потоке"""
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)

# Запускаем Flask в фоновом потоке
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()
# --- КОНЕЦ БЛОКА ДЛЯ ПОРТА ---


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

# ========== БАЗА ДАННЫХ (SQLite) ==========
def init_db():
    """Создаёт таблицы при первом запуске"""
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP
        )
    ''')
    
    # Таблица целей (КБЖУ)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS goals (
            user_id INTEGER PRIMARY KEY,
            calories INTEGER,
            protein INTEGER,
            fat INTEGER,
            carbs INTEGER,
            updated_at TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

def save_user(user_id, first_name, last_name, username):
    """Сохраняет или обновляет информацию о пользователе"""
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute('''
        INSERT INTO users (user_id, first_name, last_name, username, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            username = excluded.username,
            last_seen = excluded.last_seen
    ''', (user_id, first_name, last_name, username, now, now))
    conn.commit()
    conn.close()

def save_goals(user_id, calories, protein, fat, carbs):
    """Сохраняет или обновляет цели пользователя"""
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute('''
        INSERT INTO goals (user_id, calories, protein, fat, carbs, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            calories = excluded.calories,
            protein = excluded.protein,
            fat = excluded.fat,
            carbs = excluded.carbs,
            updated_at = excluded.updated_at
    ''', (user_id, calories, protein, fat, carbs, now))
    conn.commit()
    conn.close()

def get_goals(user_id):
    """Получает цели пользователя"""
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT calories, protein, fat, carbs FROM goals WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return {
            "calories": result[0],
            "protein": result[1],
            "fat": result[2],
            "carbs": result[3]
        }
    return None

def reset_goals(user_id):
    """Удаляет цели пользователя"""
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM goals WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

# ========== РАБОТА С ИСТОРИЕЙ ПИТАНИЯ ==========
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

def get_today_meals(user_id):
    """Возвращает сегодняшний рацион пользователя"""
    history = load_history()
    user_id_str = str(user_id)
    if user_id_str not in history:
        return None
    user_data = history[user_id_str]
    today = datetime.now().strftime("%Y-%m-%d")
    for record in user_data.get("history", []):
        if record.get("date") == today:
            return record.get("meals", "")
    return None

def save_meal_to_history(user_id, meals, products):
    history = load_history()
    user_id_str = str(user_id)
    if user_id_str not in history:
        history[user_id_str] = {"history": [], "dislikes": []}
    today = datetime.now().strftime("%Y-%m-%d")
    # Удаляем старую запись за сегодня, если есть
    history[user_id_str]["history"] = [
        r for r in history[user_id_str]["history"] 
        if r.get("date") != today
    ]
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
        # Также сбрасываем цели
        reset_goals(user_id)
        return True
    return False

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    welcome_text = (
        "🍳 *Привет! Я твой персональный кулинарный помощник!*\n\n"
        "Я помогу тебе:\n"
        "• Составить рацион из продуктов, которые есть дома\n"
        "• Рассчитать КБЖУ каждого приёма пищи\n"
        "• Предложить разнообразные блюда (не повторяюсь день ото дня)\n"
        "• Посоветовать, что можно докупить\n\n"
        "📌 *Доступные команды:*\n"
        "/set_goals — установить цели по КБЖУ на день\n"
        "/my_goals — посмотреть текущие цели\n"
        "/recipe — получить рецепт блюда из сегодняшнего рациона\n\n"
        "📌 *Как пользоваться:*\n"
        "Просто напиши список продуктов через запятую\n"
        "Или нажми кнопку «Составить рацион»\n\n"
        "Пример: *курица, гречка, яйца, помидор, огурец*"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    help_text = (
        "❓ *Помощь*\n\n"
        "📝 *Как составить рацион:*\n"
        "Напиши список продуктов через запятую или нажми кнопку «Составить рацион»\n\n"
        "🧠 *Как бот запоминает:*\n"
        "• Я помню, что ты ел вчера и позавчера\n"
        "• Стараюсь не повторять блюда\n"
        "• Меняю типы кухни и основные ингредиенты\n\n"
        "🎯 *Цели по КБЖУ:*\n"
        "/set_goals — установить лимиты калорий, белков, жиров, углеводов\n"
        "/my_goals — посмотреть текущие цели\n\n"
        "📖 *Рецепты:*\n"
        "/recipe — получить рецепт блюда из сегодняшнего рациона\n\n"
        "🗑 *Очистить историю:*\n"
        "Нажми кнопку «Очистить историю» — это сбросит память о предыдущих рационах и цели"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("clear_memory"))
async def cmd_clear_memory(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    if clear_user_history(message.from_user.id):
        await message.answer("🗑 История твоих рационов и цели по КБЖУ очищены! Теперь я буду составлять меню с чистого листа.")
    else:
        await message.answer("📭 У тебя пока нет сохранённой истории.")

# ========== КОМАНДЫ ДЛЯ ЦЕЛЕЙ (КБЖУ) ==========
@dp.message(Command("set_goals"))
async def cmd_set_goals(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    # Разбираем аргументы: /set_goals 2000 150 60 200
    args = message.text.split()[1:]
    if len(args) == 4:
        try:
            calories = int(args[0])
            protein = int(args[1])
            fat = int(args[2])
            carbs = int(args[3])
            
            if calories < 500 or calories > 5000:
                await message.answer("⚠️ Калории должны быть от 500 до 5000")
                return
            if protein < 20 or protein > 300:
                await message.answer("⚠️ Белки должны быть от 20 до 300 г")
                return
            if fat < 10 or fat > 200:
                await message.answer("⚠️ Жиры должны быть от 10 до 200 г")
                return
            if carbs < 20 or carbs > 500:
                await message.answer("⚠️ Углеводы должны быть от 20 до 500 г")
                return
            
            save_goals(user.id, calories, protein, fat, carbs)
            await message.answer(
                f"✅ *Цели сохранены!*\n\n"
                f"🔥 Калории: `{calories}` ккал\n"
                f"💪 Белки: `{protein}` г\n"
                f"🥑 Жиры: `{fat}` г\n"
                f"🍞 Углеводы: `{carbs}` г\n\n"
                "Теперь я буду учитывать их при составлении рациона!",
                parse_mode="Markdown"
            )
            return
        except ValueError:
            await message.answer("⚠️ Укажи числа: `/set_goals 2000 150 60 200`")
            return
    
    await message.answer(
        "📝 *Как установить цели:*\n"
        "`/set_goals калории белки жиры углеводы`\n\n"
        "Пример: `/set_goals 2000 150 60 200`\n\n"
        "Где:\n"
        "• калории — общая калорийность на день (500-5000)\n"
        "• белки — граммы белка (20-300)\n"
        "• жиры — граммы жиров (10-200)\n"
        "• углеводы — граммы углеводов (20-500)",
        parse_mode="Markdown"
    )

@dp.message(Command("my_goals"))
async def cmd_my_goals(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    goals = get_goals(user.id)
    if goals:
        await message.answer(
            f"🎯 *Твои текущие цели:*\n\n"
            f"🔥 Калории: `{goals['calories']}` ккал\n"
            f"💪 Белки: `{goals['protein']}` г\n"
            f"🥑 Жиры: `{goals['fat']}` г\n"
            f"🍞 Углеводы: `{goals['carbs']}` г\n\n"
            "Чтобы изменить, используй `/set_goals`",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "❌ Цели не установлены.\n"
            "Используй `/set_goals калории белки жиры углеводы`\n"
            "Пример: `/set_goals 2000 150 60 200`",
            parse_mode="Markdown"
        )

# ========== КОМАНДА /recipe (получить рецепт) ==========
@dp.message(Command("recipe"))
async def cmd_recipe(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    # Проверяем, есть ли сегодняшний рацион
    today_meals = get_today_meals(user.id)
    if not today_meals:
        await message.answer(
            "❌ У тебя ещё нет рациона на сегодня.\n"
            "Сначала отправь список продуктов, чтобы я составил меню."
        )
        return
    
    # Создаём inline-кнопки для выбора приёма пищи
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕗 Завтрак", callback_data="recipe_breakfast")],
            [InlineKeyboardButton(text="🕛 Обед", callback_data="recipe_lunch")],
            [InlineKeyboardButton(text="🕔 Перекус", callback_data="recipe_snack")],
            [InlineKeyboardButton(text="🕘 Ужин", callback_data="recipe_dinner")]
        ]
    )
    
    await message.answer(
        "📖 *Для какого приёма пищи нужен рецепт?*\n\n"
        "Выбери один из вариантов:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# Обработчик нажатия на кнопки выбора приёма пищи
@dp.callback_query(F.data.startswith("recipe_"))
async def process_recipe_callback(callback_query: types.CallbackQuery):
    meal_type = callback_query.data.replace("recipe_", "")
    
    # Маппинг для красивого вывода
    meal_names = {
        "breakfast": "Завтрак",
        "lunch": "Обед",
        "snack": "Перекус",
        "dinner": "Ужин"
    }
    
    meal_name = meal_names.get(meal_type, "приём пищи")
    
    # Получаем сегодняшний рацион
    today_meals = get_today_meals(callback_query.from_user.id)
    
    if not today_meals:
        await callback_query.message.edit_text(
            "❌ Рацион на сегодня не найден. Сначала отправь список продуктов."
        )
        await callback_query.answer()
        return
    
    # Ищем нужный приём пищи в тексте рациона
    # Используем эмодзи для поиска
    emoji_map = {
        "breakfast": "🕗 ЗАВТРАК",
        "lunch": "🕛 ОБЕД",
        "snack": "🕔 ПЕРЕКУС",
        "dinner": "🕘 УЖИН"
    }
    
    search_pattern = emoji_map.get(meal_type)
    
    # Находим блок текста для нужного приёма пищи
    lines = today_meals.split('\n')
    meal_text = ""
    found = False
    
    for i, line in enumerate(lines):
        if search_pattern in line.upper():
            found = True
            meal_text += line + '\n'
            # Собираем следующие строки до следующего приёма пищи
            for j in range(i + 1, len(lines)):
                next_line = lines[j]
                # Проверяем, не начался ли следующий приём
                is_next_meal = False
                for emoji in ["🕗", "🕛", "🕔", "🕘"]:
                    if emoji in next_line:
                        is_next_meal = True
                        break
                if is_next_meal:
                    break
                meal_text += next_line + '\n'
            break
    
    if not found or not meal_text.strip():
        await callback_query.message.edit_text(
            f"❌ Не удалось найти {meal_name.lower()} в сегодняшнем рационе.\n"
            "Попробуй снова: /recipe"
        )
        await callback_query.answer()
        return
    
    # Теперь запрашиваем рецепт у DeepSeek
    processing_msg = await callback_query.message.edit_text(
        f"👨‍🍳 Готовлю рецепт для *{meal_name}*...\n\n"
        f"Вот что у нас есть:\n{meal_text}",
        parse_mode="Markdown"
    )
    
    prompt = f"""Ты — шеф-повар. Напиши подробный рецепт для этого блюда.

ВОТ БЛЮДО:
{meal_text}

Напиши рецепт в формате:
📝 *Название блюда:*
🛒 *Ингредиенты:*
• ингредиент 1 — количество
• ингредиент 2 — количество

👨‍🍳 *Приготовление:*
1. Шаг 1
2. Шаг 2
3. Шаг 3

💡 *Совет:* (опционально)

Рецепт должен быть понятным, с конкретными количествами и временем приготовления. Используй только эмодзи для оформления, без символов * _ ` ~."""
    
    try:
        response = await deepseek.chat.completions.create(
            model="openai/gpt-oss-120b:free",
            messages=[
                {"role": "system", "content": "Ты — профессиональный шеф-повар. Отвечай без использования символов форматирования типа * _ ` ~. Используй только эмодзи."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=60.0
        )
        
        recipe = response.choices[0].message.content
        recipe = re.sub(r'[*_`~]', '', recipe)
        
        # Убираем лишние эмодзи из текста рецепта, если они есть
        await callback_query.message.edit_text(
            f"📖 *Рецепт: {meal_name}*\n\n{recipe}",
            parse_mode="Markdown"
        )
        
    except asyncio.TimeoutError:
        await callback_query.message.edit_text("⏰ Превышено время ожидания. Попробуй ещё раз.")
    except Exception as e:
        await callback_query.message.edit_text(f"❌ Ошибка: {str(e)}\nПопробуй ещё раз.")
    
    await callback_query.answer()

# ========== СКРЫТАЯ КОМАНДА /stats (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРА) ==========
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    ADMIN_ID = 5179439405  # Ваш ID
    
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return
    
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    # Общее количество пользователей
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    # Новые за сегодня
    today = datetime.now().date()
    cursor.execute("SELECT COUNT(*) FROM users WHERE DATE(last_seen) = ?", (today,))
    today_users = cursor.fetchone()[0]
    
    # Активные за последние 7 дней
    week_ago = datetime.now().date()
    cursor.execute("SELECT COUNT(*) FROM users WHERE DATE(last_seen) >= ?", (week_ago,))
    week_users = cursor.fetchone()[0]
    
    # Количество пользователей с установленными целями
    cursor.execute("SELECT COUNT(*) FROM goals")
    goals_users = cursor.fetchone()[0]
    
    conn.close()
    
    await message.answer(
        f"📊 *Статистика бота*\n\n"
        f"👥 Всего пользователей: `{total_users}`\n"
        f"🆕 За сегодня: `{today_users}`\n"
        f"📅 За 7 дней: `{week_users}`\n"
        f"🎯 С целями КБЖУ: `{goals_users}`",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📋 Составить рацион")
async def button_generate(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    await message.answer("📝 Напиши список продуктов, которые есть дома, через запятую\n\nПример: *курица, рис, помидоры, яйца, лук*", parse_mode="Markdown")

@dp.message(F.text == "🗑 Очистить историю")
async def button_clear(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    if clear_user_history(message.from_user.id):
        await message.answer("🗑 История твоих рационов и цели по КБЖУ очищены!")
    else:
        await message.answer("📭 У тебя пока нет сохранённой истории.")

@dp.message(F.text == "❓ Помощь")
async def button_help(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    await cmd_help(message)

# ========== ГЕНЕРАЦИЯ РАЦИОНА ==========
@dp.message(F.text)
async def generate_meal_plan(message: types.Message):
    user = message.from_user
    save_user(user.id, user.first_name, user.last_name, user.username)
    
    user_id = message.from_user.id
    products = message.text.strip()
    
    # Проверяем, не является ли текст командой
    if products.startswith('/'):
        return
    
    processing_msg = await message.answer("🤔 Анализирую продукты и вспоминаю, что ты ел вчера...")
    
    context = get_user_context(user_id)
    recent_meals = context["recent_meals"]
    dislikes = context["dislikes"]
    goals = get_goals(user_id)
    
    # Строим промпт с учётом целей
    prompt = f"""Ты — профессиональный нутрициолог и шеф-повар. Помоги пользователю составить разнообразный рацион на сегодня.

ПРОДУКТЫ В НАЛИЧИИ: {products}

"""

    if goals:
        prompt += f"""ЦЕЛИ ПО КБЖУ НА ДЕНЬ (СТАРАЙСЯ ПРИДЕРЖИВАТЬСЯ):
• Калории: {goals['calories']} ккал
• Белки: {goals['protein']} г
• Жиры: {goals['fat']} г
• Углеводы: {goals['carbs']} г

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
6. Старайся соответствовать целям по КБЖУ, если они установлены

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
        
        # Добавляем подсказку про /recipe
        await message.answer(
            "💡 *Совет:* Чтобы получить рецепт любого блюда из этого рациона, отправь команду `/recipe` и выбери приём пищи.\n\n"
            "Я запомнил этот рацион. Завтра предложу что-то новое, чтобы тебе не надоедало!\n"
            "Напиши /clear_memory если хочешь сбросить историю.",
            parse_mode="Markdown"
        )
        
    except asyncio.TimeoutError:
        await processing_msg.edit_text("⏰ Превышено время ожидания ответа. Попробуй ещё раз.")
    except Exception as e:
        await processing_msg.edit_text(f"❌ Ошибка: {str(e)}\nПопробуй ещё раз.")

# ========== ЗАПУСК БОТА ==========
async def main():
    print("🤖 Бот запущен и готов к работе!")
    print(f"📁 Файл истории: {HISTORY_FILE}")
    
    # Инициализируем базу данных
    init_db()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
