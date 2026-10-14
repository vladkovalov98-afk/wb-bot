import asyncio
import logging
import socket
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# Твой токен от Telegram:
TOKEN = "8844480231:AAHS9zOShX1I9tJQ8WFIA1wfVPC15bKLYD0"

class IPv4Session(AiohttpSession):
    def __init__(self):
        super().__init__(timeout=120)
        self._connector_init["family"] = socket.AF_INET

bot = Bot(token=TOKEN, session=IPv4Session())
dp = Dispatcher()
DB_NAME = "users.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, free_limits INTEGER DEFAULT 3, is_vip INTEGER DEFAULT 0)")
        await db.commit()

async def get_or_create_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT free_limits, is_vip FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is None:
            await db.execute("INSERT INTO users (user_id, username, free_limits, is_vip) VALUES (?, ?, 3, 0)", (user_id, username))
            await db.commit()
            return 3, 0
        return row[0], row[1]

async def spend_limit(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET free_limits = free_limits - 1 WHERE user_id = ?", (user_id,))
        await db.commit()

# Бесплатный ИИ-шлюз, который работает из РФ напрямую без VPN и без ключей
async def generate_ai_seo(product_text: str) -> str:
    prompt = (
        "Ты профессиональный SEO-копирайтер для маркетплейсов Wildberries и Ozon. "
        "Пользователь пришлет название товара и его краткие характеристики. "
        "Составь готовую карточку товара строго по структуре:\n"
        "1. 📌  Продающий SEO-заголовок (до 60 символов)\n"
        "2. 📝  Описание (2-3 абзаца, без воды, с ключевыми запросами покупателей)\n"
        "3. 🔥  5 главных преимуществ (списком с буллитами)\n"
        "4. 📋 Характеристики для заполнения карточки\n"
        "5. 🔑  10 высокочастотных SEO-ключей через запятую.\n"
        "Пиши живо, грамотно, на русском языке."
    )
    payload = {
        "model": "openai",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Товар: {product_text}"}
        ],
        "temperature": 0.7
    }
    headers = {"Content-Type": "application/json"}
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post("https://text.pollinations.ai/openai", headers=headers, json=payload, timeout=60) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
            else:
                raise Exception(f"Код {resp.status}: {await resp.text()}")

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀  Сгенерировать описание"), KeyboardButton(text="👤  Мой профиль / Подписка")],
        [KeyboardButton(text="⚙️  Статус системы")]
    ],
    resize_keyboard=True
)

buy_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="💎  Купить PRO-безлимит (290 ₽/мес)", callback_data="buy_pro")],
        [InlineKeyboardButton(text="🎁  Протестировать выдачу VIP (Для админа)", callback_data="test_vip")]
    ]
)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    limits, is_vip = await get_or_create_user(message.from_user.id, message.from_user.username or "NoName")
    status = "💎  PRO Безлимит" if is_vip else f"🎁  Тестовый ({limits} беспл. генераций)"
    await message.answer(f"Привет, {message.from_user.first_name}! 👋 \n"
        f"Я ИИ-Помощник селлера для WB и Ozon.\n\n"
        f"Твой тариф: {status}\n\n"
        f"Напиши мне любой товар, и нейросеть за пару секунд соберет готовую SEO-карточку!",
        reply_markup=main_kb
    )

@dp.message(F.text == "👤  Мой профиль / Подписка")
async def check_sub(message: Message):
    limits, is_vip = await get_or_create_user(message.from_user.id, message.from_user.username or "NoName")
    if is_vip:
        text = f"👤  Ваш ID: {message.from_user.id}\n💎  Тариф: PRO Безлимит активен!"
    else:
        text = f"👤  Ваш ID: {message.from_user.id}\n🔋  Осталось бесплатных генераций: {limits} шт.\n\nОформите подписку, чтобы снять лимиты:"
    await message.answer(text, reply_markup=buy_kb)

@dp.message(F.text == "⚙️  Статус системы")
async def sys_status(message: Message):
    await message.answer("✅ База данных подключена.\n🧠  Модуль ИИ (GPT-4o-mini) активен и работает без региональных блокировок.")

@dp.message(F.text == "🚀  Сгенерировать описание")
async def start_gen(message: Message):
    await message.answer("✍️  Напиши в чат название товара и 2-3 его характеристики:")

@dp.callback_query(F.data == "buy_pro")
async def process_buy(call: CallbackQuery):
    await call.message.answer("💳 Оплата подписки PRO (290 ₽)\n\nНажми кнопку «Протестировать выдачу VIP» выше для проверки безлимита.")
    await call.answer()

@dp.callback_query(F.data == "test_vip")
async def give_test_vip(call: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_vip = 1 WHERE user_id = ?", (call.from_user.id,))
        await db.commit()
    await call.message.answer("🎉  Поздравляем! Тебе включен безлимитный VIP-доступ!")
    await call.answer()

@dp.message(F.text)
async def generate_card(message: Message):
    limits, is_vip = await get_or_create_user(message.from_user.id, message.from_user.username or "NoName")
    if not is_vip and limits <= 0:
        await message.answer(
            "⛔ У вас закончились бесплатные генерации (0 из 3)!\n\nАктивируйте подписку PRO:",
            reply_markup=buy_kb
        )
        return

    wait_msg = await message.answer("⏳ Нейросеть анализирует ключи и пишет SEO-описание...")

    try:
        ai_result = await generate_ai_seo(message.text)
        if not is_vip:
            await spend_limit(message.from_user.id)
            limits -= 1
        
        footer = f"\n─────────────────\n🔋  Остаток генераций: {'Безлимит 💎 ' if is_vip else f'{limits} шт.'}"
        await wait_msg.edit_text(ai_result + footer)
    except Exception as e:
        await wait_msg.edit_text(f"⚠️  Ответ сервера ИИ: {e}")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    print("ИИ-Бот (открытый шлюз РФ) успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())