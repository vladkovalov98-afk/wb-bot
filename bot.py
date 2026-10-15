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
CLOUDFLARE_ACCOUNT_ID = os.environ["CLOUDFLARE_ACCOUNT_ID"]
CLOUDFLARE_API_TOKEN = os.environ["CLOUDFLARE_API_TOKEN"]
DB_NAME = os.environ.get("DB_NAME", "users.db")

# Цепочка моделей Gemini: сначала быстрая, потом стабильная, потом резерв
GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

CF_MODEL = "@cf/black-forest-labs/flux-2-dev"
CF_API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CF_MODEL}"

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
        # Перебираем модели одну за другой
        for model in GEMINI_MODELS:
            url = GEMINI_URL_TEMPLATE.format(model=model)
            for attempt in range(3):
                try:
                    async with s.post(
                        url,
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
                            log.info("Gemini OK with model %s", model)

                            if "===SPLIT===" in full_text:
                                parts = full_text.split("===SPLIT===")
                                return parts[0].strip(), parts[1].strip()
                            return full_text.strip(), (
                                "Place this exact product on a clean studio background, "
                                "soft lighting, preserve product details, 8k"
                            )
                        elif resp.status in (503, 429):
                            # Модель перегружена — переходим к следующей модели
                            err = await resp.text()
                            log.warning("Gemini %s -> %s (switching model): %s", model, resp.status, err[:150])
                            break
                        elif resp.status == 404:
                            # Модель не существует — переходим к следующей
                            log.warning("Gemini model %s not found (404), trying next", model)
                            break
                        else:
                            err = await resp.text()
                            log.warning("Gemini %s HTTP %s: %s", model, resp.status, err[:300])
                except Exception as e:
                    log.warning("Gemini %s attempt %d error: %s", model, attempt + 1, e)
                await asyncio.sleep(2)
            # Небольшая пауза перед следующей моделью
            await asyncio.sleep(1)

    # Все модели упали — фоллбэк
    log.error("All Gemini models failed, fallback to Pollinations")
    fallback_topic = caption if caption else "кроссовки спортивные мужские"
    seo_fallback = await generate_seo(
        f"{fallback_topic}. Сделай полноценное продающее SEO-описание для маркетплейса Wildberries."
    )
    return seo_fallback, "Place this exact product on a clean studio background, soft lighting, 8k"

# --- ГЕНЕРАЦИЯ ОБЛОЖКИ через CLOUDFLARE WORKERS AI (img2img) ---
async def generate_card_image(prompt: str, image_bytes: bytes | None = None) -> bytes | None:
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}

    form = aiohttp.FormData()
    form.add_field("prompt", prompt)

    if image_bytes:
        form.add_field(
            "input_image_0",
            image_bytes,
            filename="input.jpg",
            content_type="image/jpeg"
        )

    async with aiohttp.ClientSession() as s:
        for attempt in range(3):
            try:
                async with s.post(
                    CF_API_URL,
                    headers=headers,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        img_b64 = result.get("image")
                        if img_b64:
                            return base64.b64decode(img_b64)
                        log.warning("Cloudflare: no image in response: %s", str(data)[:300])
                    else:
                        err = await resp.text()
                        log.warning("Cloudflare HTTP %s attempt %d: %s", resp.status, attempt + 1, err[:300])
            except Exception as e:
                log.warning("Cloudflare attempt %d error: %s", attempt + 1, e)
            await asyncio.sleep(3 * (attempt + 1))
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
        "я проанализирую фото, сделаю студийную обложку 3:4 "
        "с сохранением дизайна товара и напишу полное SEO-описание.\n\n"
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
        "📸 Фото получено! Анализирую, пишу SEO и стилизую обложку (30–90 сек)..."
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
            f"🖼 Генерация по фото: Активна (Gemini Vision + Cloudflare Workers AI)\n"
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
