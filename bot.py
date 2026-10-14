import asyncio
import logging
import os
import urllib.parse
import aiohttp
from aiohttp import web
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery

# --- НАСТРОЙКИ БОТА ---
TOKEN = "8844480231:AAF8L_uM1R9C1W5R3_xG-zI4U8pY2wK6vQo"  # Проверь свой токен!
ADMIN_ID = 944857374
DB_NAME = "users.db"
SUBSCRIPTION_PRICE_STARS = 150  # Цена PRO-доступа в Telegram Stars (экв. ~290-300 ₽)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                free_left INTEGER DEFAULT 3,
                is_pro INTEGER DEFAULT 0
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT free_left, is_pro FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await db.execute("INSERT INTO users (user_id, free_left, is_pro) VALUES (?, 3, 0)", (user_id,))
                await db.commit()
                return 3, 0
            return row[0], row[1]

async def use_attempt(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET free_left = free_left - 1 WHERE user_id = ? AND is_pro = 0", (user_id,))
        await db.commit()

async def set_pro(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_pro = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

# --- ИИ ГЕНЕРАТОР SEO (Бесплатный шлюз Pollinations AI) ---
async def generate_seo(product_text: str) -> str:
    system_prompt = (
        "Ты профессиональный SEO-копирайтер для маркетплейсов Wildberries и Ozon. "
        "Напиши продающее SEO-описание карточки товара на русском языке. "
        "Структура ответа:\n"
        "1. Продающий заголовок (до 60 символов)\n"
        "2. Описание товара (1000-1500 символов с ключевыми словами)\n"
        "3. 5 главных преимуществ (буллитами)\n"
        "4. Блок из 10-15 ключевых запросов через запятую.\n"
        "Пиши живо, без воды и без символов разметки Markdown вроде ** или ##."
    )
    url = "https://text.pollinations.ai/"
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Товар: {product_text}"}
        ],
        "model": "openai"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=60) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return text.replace("**", "").replace("##", "").replace("###", "")
                else:
                    # Резервный GET-запрос, если POST перегружен
                    encoded = urllib.parse.quote(f"{system_prompt}\nТовар: {product_text}")
                    async with session.get(f"https://text.pollinations.ai/{encoded}", timeout=60) as resp2:
                        return (await resp2.text()).replace("**", "").replace("##", "")
    except Exception as e:
        return f"⚠️  Сервер ИИ временно перегружен, попробуй еще раз через пару секунд! ({e})"

# --- КЛАВИАТУРЫ ---
def get_buy_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💎  Купить PRO-безлимит ({SUBSCRIPTION_PRICE_STARS} ⭐)", callback_data="buy_stars")]
    ])

# --- ОБРАБОТЧИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    free_left, is_pro = await get_user(message.from_user.id)
    status = "💎  PRO Безлимит" if is_pro else f"🎁  Бесплатных генераций: {free_left}"
    await message.answer(
        f"👋  Привет! Я ИИ-помощник для селлеров **Wildberries и Ozon**.\n\n"
        f"Напиши мне название товара и его краткие характеристики (например: *Платье женское летнее хлопок, размеры 42-48*), "
        f"и я за 10 секунд соберу готовую продающую SEO-карточку!\n\n"
        f"Твой статус: {status}",
        reply_markup=None if is_pro else get_buy_kb()
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*), SUM(is_pro) FROM users") as cur:
            total, pros = await cur.fetchone()
    await message.answer(f"📊  **Админ-панель**\nВсего пользователей: {total}\nPRO-подписок: {pros or 0}")

# --- ОПЛАТА TELEGRAM STARS ---
@dp.callback_query(F.data == "buy_stars")
async def process_buy_stars(callback: types.CallbackQuery):
    await callback.message.answer_invoice(
        title="💎  PRO-безлимит на генерацию SEO",
        description="Полный безлимит на создание продающих SEO-описаний для Wildberries и Ozon.",
        payload="pro_sub_stars",
        provider_token="",  # Для Telegram Stars (XTR) всегда пустая строка
        currency="XTR",
        prices=[LabeledPrice(label="PRO Безлимит", amount=SUBSCRIPTION_PRICE_STARS)]
    )
    await callback.answer()

@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    await set_pro(message.from_user.id)
    await message.answer("🎉  **Оплата прошла успешно!**\nТебе активирован 💎  **PRO-безлимит**! Отправляй названия товаров без ограничений.")

# --- ГЕНЕРАЦИЯ ПО ТЕКСТУ ---
@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    free_left, is_pro = await get_user(user_id)

    if not is_pro and free_left <= 0:
        await message.answer(
            f"🔒 Бесплатные генерации закончились!\n\n"
            f"Подключи **PRO-безлимит** всего за **{SUBSCRIPTION_PRICE_STARS} ⭐** и создавай описания без ограничений:",
            reply_markup=get_buy_kb()
        )
        return

    wait_msg = await message.answer("⏳ Генерирую продающее SEO-описание для WB и Ozon... Подожди 10–15 секунд.")
    seo_result = await generate_seo(message.text)

    if not is_pro:
        await use_attempt(user_id)

    await wait_msg.edit_text(seo_result)

# --- ВЕБ-ЗАГЛУШКА ДЛЯ БЕСПЛАТНОГО ХОСТИНГА (KOYEB / RENDER) ---
async def health_check(request):
    return web.Response(text="WB SEO Bot is running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")

async def main():
    await init_db()
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
