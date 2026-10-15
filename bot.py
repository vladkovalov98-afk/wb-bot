import asyncio
import base64
import io
import logging
import os
import urllib.parse
import aiohttp
import aiosqlite

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from aiogram.client.session.aiohttp import AiohttpSession

# --- НАСТРОЙКИ ---
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
AGNES_API_KEY = os.environ["AGNES_API_KEY"]
DB_NAME = os.environ.get("DB_NAME", "users.db")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
AGNES_IMG_URL = "https://apihub.agnes-ai.com/v1/images/generations"

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

# --- SEO ПО ТЕКСТУ (Pollinations) ---
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

# --- VISION через GEMINI: ФОТО → SEO + ПРОМПТ ДЛЯ ОБЛОЖКИ ---
async def analyze_photo_and_make_seo(image_bytes: bytes, caption: str = "") -> tuple[str, str]:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    user_hint = f"Товар на фото: {caption}." if caption else ""

    prompt = (
        "Ты эксперт по маркетплейсам Wildberries и Ozon. "
        "Посмотри ВНИМАТЕЛЬНО на фото и определи, что за товар на нём изображён "
        "(тип товара, цвет, материал, ключевые детали). Отвечай СТРОГО в двух частях, "
        "разделённых меткой ===SPLIT===:\n\n"
        "Часть 1 (на русском): Продающее SEO-описание карточки для ЭТОГО товара "
        "(Заголовок до 60 символов, Описание 800-1200 символов, 5 преимуществ буллитами, "
        "10-15 ключевых запросов через запятую). Без символов ** и ##.\n\n"
        "===SPLIT===\n\n"
        "Часть 2 (СТРОГО НА АНГЛИЙСКОМ, до 30 слов): Instruction for image editing. "
        "Формат: 'Place this exact product on a clean studio background with soft lighting, "
        "preserve the product shape, color and details, professional e-commerce photo, 8k'. "
        "НЕ описывай сам товар — только инструкцию по стилизации фона и освещения.\n\n"
        f"{user_hint}"
    )

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 3000
        }
    }

    async with aiohttp.ClientSession() as s:
        for attempt in range(3):
            try:
                async with s.post(
                    GEMINI_URL,
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            full_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        except (KeyError, IndexError) as e:
                            log.error("Gemini parse error: %s | data=%s", e, str(data)[:300])
                            await asyncio.sleep(2)
                            continue

                        full_text = full_text.replace("**", "").replace("##", "").replace("###", "")

                        if "===SPLIT===" in full_text:
                            parts = full_text.split("===SPLIT===")
                            return parts[0].strip(), parts[1].strip()
                        return full_text.strip(), (
                            "Place this exact product on a clean studio background, "
                            "soft lighting, preserve product details, 8k"
                        )
                    else:
                        err = await resp.text()
                        log.warning("Gemini HTTP %s: %s", resp.status, err[:300])
            except Exception as e:
                log.warning("Gemini attempt %d error: %s", attempt + 1, e)
            await asyncio.sleep(2)

    # Фоллбэк
    log.error("Gemini vision failed, fallback to text SEO")
    fallback_item = caption if caption else "товар на фото"
    seo_fallback = await generate_seo(fallback_item)
    return seo_fallback, "Place this exact product on a clean studio background, soft lighting, 8k"

# --- ГЕНЕРАЦИЯ ОБЛОЖКИ через AGNES AI (img2img) ---
async def generate_card_image(prompt: str, image_bytes: bytes | None = None) -> bytes | None:
    payload = {
        "model": "agnes-image-2.0-flash",
        "prompt": prompt,
        "size": "768x1024",
        "extra_body": {
            "response_format": "b64_json"
        }
    }

    if image_bytes:
        b64_input = base64.b64encode(image_bytes).decode("utf-8")
        payload["extra_body"]["image"] = [f"data:image/jpeg;base64,{b64_input}"]

    async with aiohttp.ClientSession() as s:
        for attempt in range(5):
            try:
                async with s.post(
                    AGNES_IMG_URL,
                    headers={
                        "Authorization": f"Bearer {AGNES_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=90)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        b64_out = data["data"][0].get("b64_json")
                        if b64_out:
                            return base64.b64decode(b64_out)
                        log.warning("Agnes: no b64_json in response")
                    else:
                        err = await resp.text()
                        log.warning("Agnes HTTP %s attempt %d: %s", resp.status, attempt + 1, err[:300])
            except Exception as e:
                log.warning("Agnes attempt %d error: %s", attempt + 1, e)
            # Экспоненциальная пауза: 5, 10, 15, 20 секунд
            await asyncio.sleep(5 * (attempt + 1))
    return None

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await get_user(message.from_user.id)
    await message.answer(
        "👋 Привет! Я ИИ-помощник для селлеров Wildberries и Ozon.\n\n"
        "📌 Что я умею:\n"
        "1️⃣ Отправь мне текстом название товара — я соберу SEO-описание.\n"
        "2️⃣ Отправь мне фотографию товара (лучше с короткой подписью) — "
        "я проанализирую фото через Gemini Vision, сделаю студийную обложку 3:4 "
        "с сохранением дизайна товара (Agnes AI) и напишу полное SEO-описание.\n\n"
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
        "📸 Фото получено! Анализирую через Gemini Vision, пишу SEO и стилизую обложку (30–90 сек)..."
    )

    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_stream = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=file_stream)
        image_bytes = file_stream.getvalue()

        caption = message.caption or ""
        seo_text, img_prompt = await analyze_photo_and_make_seo(image_bytes, caption)

        log.info("Image prompt: %s", img_prompt)
        generated_img_bytes = await generate_card_image(img_prompt, image_bytes)

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
            f"🖼 Генерация по фото: Активна (Gemini Vision + Agnes AI)\n"
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
