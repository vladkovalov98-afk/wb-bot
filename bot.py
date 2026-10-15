import asyncio
import base64
import io
import logging
import os
import random
import urllib.parse
import aiohttp
import aiosqlite

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from aiogram.client.session.aiohttp import AiohttpSession

# --- НАСТРОЙКИ ---
TOKEN = os.environ["BOT_TOKEN"]                    # добавить в Railway → Variables
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))    # добавить в Railway → Variables
DB_NAME = os.environ.get("DB_NAME", "users.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=TOKEN, session=AiohttpSession())
dp = Dispatcher()

# --- КЛАВИАТУРА ---
BTN_GEN = "🚀 Сгенерировать описание"
BTN_PROFILE = "👤 Мой профиль / Подписка"
BTN_STATUS = "⚙️ Статус системы"

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_GEN), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_STATUS)]
    ],
    resize_keyboard=True
)

# --- БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                free_left INTEGER DEFAULT 999999,
                is_pro INTEGER DEFAULT 1
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT free_left, is_pro FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await db.execute("INSERT INTO users (user_id, free_left, is_pro) VALUES (?, 999999, 1)", (user_id,))
                await db.commit()
                return 999999, 1
            return row[0], row[1]

# --- SEO ПО ТЕКСТУ ---
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
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Товар: {product_text}"}
        ],
        "model": "openai"
    }

    async with aiohttp.ClientSession() as s:
        for attempt in range(3):
            try:
                async with s.post(
                    "https://text.pollinations.ai/",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=40)
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        if len(text.strip()) > 10:
                            return text.replace("**", "").replace("##", "").replace("###", "").strip()
                    log.warning("SEO HTTP %s", resp.status)
            except Exception as e:
                log.warning("SEO attempt %d error: %s", attempt + 1, e)
            await asyncio.sleep(1.5)

        # Резервный GET
        try:
            short = (
                f"Напиши продающее SEO-описание для Wildberries и Ozon "
                f"(заголовок, описание, 5 преимуществ, ключевые слова) для товара: {product_text}"
            )
            url = "https://text.pollinations.ai/" + urllib.parse.quote(short) + "?model=openai"
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=40)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return text.replace("**", "").replace("##", "").replace("###", "").strip()
        except Exception as e:
            log.error("SEO GET fallback error: %s", e)

    return "⚠️ Сервер ИИ временно перегружен. Попробуй ещё раз через 10 секунд."

# --- VISION: ФОТО → SEO + ПРОМПТ ДЛЯ ОБЛОЖКИ ---
async def analyze_photo_and_make_seo(image_bytes: bytes, caption: str = "") -> tuple[str, str]:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    user_hint = f"Товар на фото: {caption}" if caption else "Определи товар на фото самостоятельно."

    system_prompt = (
        "Ты эксперт по маркетплейсам Wildberries и Ozon. Посмотри на фото товара и выдай ответ строго из двух частей, разделённых меткой ===SPLIT===:\n"
        "Часть 1 (на русском): Продающее SEO-описание карточки (Заголовок, Описание, 5 преимуществ, Ключевые слова). Без символов ** и ##.\n"
        "===SPLIT===\n"
        "Часть 2 (НА АНГЛИЙСКОМ, до 30 слов): Промпт для генерации студийного рекламного фото этого товара "
        "(commercial product photography, studio lighting, clean luxury background, 8k)."
    )

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_hint},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}}
                ]
            }
        ],
        "model": "openai"
    }

    async with aiohttp.ClientSession() as s:
        for attempt in range(3):
            try:
                async with s.post(
                    "https://text.pollinations.ai/",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=50)
                ) as resp:
                    if resp.status == 200:
                        full_text = (await resp.text()).replace("**", "").replace("##", "").replace("###", "")
                        if "===SPLIT===" in full_text:
                            parts = full_text.split("===SPLIT===")
                            return parts[0].strip(), parts[1].strip()
                        return full_text.strip(), (
                            f"Professional commercial product photography of "
                            f"{caption or 'modern product'}, studio lighting, luxury background, 8k"
                        )
                    log.warning("Vision HTTP %s", resp.status)
            except Exception as e:
                log.warning("Vision attempt %d error: %s", attempt + 1, e)
            await asyncio.sleep(2)

    # Фоллбэк: хотя бы SEO по подписи + универсальный промпт
    fallback_item = caption if caption else "Тактические кроссовки / обувь"
    seo_fallback = await generate_seo(fallback_item)
    prompt_fallback = (
        f"Commercial product photography of {caption or 'modern product'}, "
        f"studio lighting, podium background, ultra detailed, 8k"
    )
    return seo_fallback, prompt_fallback

# --- ГЕНЕРАЦИЯ ОБЛОЖКИ 3:4 ---
async def generate_card_image(prompt: str) -> bytes | None:
    clean_prompt = urllib.parse.quote(prompt[:250])
    seed = random.randint(1000, 999999)
    url = (
        f"https://image.pollinations.ai/prompt/{clean_prompt}"
        f"?width=768&height=1024&seed={seed}&nologo=true&model=flux"
    )

    async with aiohttp.ClientSession() as s:
        for _ in range(3):
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 1000:
                            return data
            except Exception as e:
                log.warning("Image gen error: %s", e)
            await asyncio.sleep(2)
    return None

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await get_user(message.from_user.id)
    await message.answer(
        "👋 Привет! Я ИИ-помощник для селлеров Wildberries и Ozon.\n\n"
        "📌 Что я умею:\n"
        "1️⃣ Отправь мне текстом название товара — я соберу SEO-описание.\n"
        "2️⃣ Отправь мне фотографию товара (лучше с короткой подписью, например: «Кроссовки мужские хаки») — "
        "я сделаю готовую студийную обложку 3:4 для карточки и напишу полное SEO-описание!\n\n"
        "✨ У тебя активен полный безлимит на все генерации.",
        reply_markup=main_keyboard
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            (total,) = await cur.fetchone()
    await message.answer(f"📊 Админ-панель\n\nВсего пользователей: {total}\nРежим: Безлимит для всех")

# --- ФОТО ---
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    await get_user(user_id)

    wait_msg = await message.answer(
        "📸 Фото получено! Анализирую товар, пишу SEO-описание и создаю обложку для карточки (20–35 сек)..."
    )

    try:
        photo = message.photo[1] if len(message.photo) > 1 else message.photo[0]
        file_info = await bot.get_file(photo.file_id)
        file_stream = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=file_stream)
        image_bytes = file_stream.getvalue()

        caption = message.caption or ""
        seo_text, img_prompt = await analyze_photo_and_make_seo(image_bytes, caption)

        generated_img_bytes = await generate_card_image(img_prompt)

        if generated_img_bytes:
            photo_file = BufferedInputFile(generated_img_bytes, filename="wb_ozon_card.jpg")
            await message.answer_photo(
                photo=photo_file,
                caption="🎨 Готовая студийная обложка для карточки товара (формат 3:4)!"
            )
            await wait_msg.delete()
            await message.answer(seo_text, reply_markup=main_keyboard)
        else:
            await wait_msg.edit_text(
                f"⚠️ Сервер графики перегружен, но вот SEO-описание:\n\n{seo_text}"
            )
    except Exception as e:
        log.exception("handle_photo error")
        await wait_msg.edit_text(f"⚠️ Произошла ошибка при обработке фото: {e}")

# --- ТЕКСТ И КНОПКИ ---
@dp.message(F.text)
async def handle_all_text(message: types.Message):
    text = message.text.strip()
    user_id = message.from_user.id

    if text == BTN_STATUS or "Статус системы" in text:
        await get_user(user_id)
        await message.answer(
            f"📊 Статус системы\n\n"
            f"🆔 Ваш ID: {user_id}\n"
            f"✨ Статус: 💎 PRO Безлимит\n"
            f"🖼 Генерация по фото: Активна\n"
            f"🚀 Все системы работают в штатном режиме.",
            reply_markup=main_keyboard
        )
        return

    if text == BTN_PROFILE or "Мой профиль" in text:
        await get_user(user_id)
        await message.answer(
            f"👤 Ваш профиль\n\n"
            f"• Telegram ID: {user_id}\n"
            f"• Текущий тариф: 💎 PRO Безлимит (Активен)\n"
            f"• Доступно: Текстовое SEO + Генерация карточек по фото (∞).",
            reply_markup=main_keyboard
        )
        return

    if text == BTN_GEN or "Сгенерировать описание" in text:
        await message.answer(
            "✍️ Отправь мне:\n"
            "• Текст (название и характеристики товара)\n"
            "• Или Фотографию товара (можно с подписью)\n\n"
            "И я подготовлю SEO-описание и обложку для карточки!",
            reply_markup=main_keyboard
        )
        return

    if len(text) > 2000:
        await message.answer("Слишком длинный текст, сократи до 2000 символов.")
        return

    await get_user(user_id)
    wait_msg = await message.answer("⏳ Генерирую продающее SEO-описание для WB и Ozon...")
    seo_result = await generate_seo(text)

    if len(seo_result) <= 4000:
        await wait_msg.edit_text(seo_result)
    else:
        await wait_msg.edit_text(seo_result[:4000])
        for i in range(4000, len(seo_result), 4000):
            await message.answer(seo_result[i:i + 4000])

# --- ЗАПУСК ---
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
