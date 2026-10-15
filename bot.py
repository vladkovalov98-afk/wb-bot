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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")
DB_NAME = os.environ.get("DB_NAME", "users.db")

GEMINI_MODELS = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Replicate — FLUX Kontext Pro
REPLICATE_MODEL = "black-forest-labs/flux-kontext-pro"
REPLICATE_URL = f"https://api.replicate.com/v1/models/{REPLICATE_MODEL}/predictions"

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

VISION_PROMPT = (
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
    "НЕ описывай сам товар — только инструкцию по стилизации."
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

# --- SEO по тексту (Pollinations) ---
async def generate_seo(product_text: str) -> str:
    system_prompt = (
        "Ты профессиональный SEO-копирайтер для маркетплейсов Wildberries и Ozon. "
        "Напиши продающее SEO-описание карточки товара на русском языке. "
        "Структура ответа:\n"
        "1. Продающий заголовок (до 60 символов)\n"
        "2. Описание товара (1000-1500 символов с ключевыми словами)\n"
        "3. 5 главных преимуществ (буллитами)\n"
        "4. Блок из 10-15 ключевых запросов через запятую.\n"
        "Пиши живо, без воды и без символов ** и ##."
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
            except Exception as e:
                log.warning("SEO attempt %d error: %s", attempt + 1, e)
            await asyncio.sleep(1.5)

        try:
            short = f"Напиши продающее SEO-описание для Wildberries и Ozon для товара: {product_text}"
            url = "https://text.pollinations.ai/" + urllib.parse.quote(short) + "?model=openai"
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=40)) as resp:
                if resp.status == 200:
                    return (await resp.text()).replace("**", "").replace("##", "").replace("###", "").strip()
        except Exception as e:
            log.error("SEO GET error: %s", e)

    return "⚠️ Сервер ИИ временно перегружен. Попробуй ещё раз через 10 секунд."

def parse_vision_response(full_text: str) -> tuple[str, str]:
    full_text = full_text.replace("**", "").replace("##", "").replace("###", "").strip()
    if "===SPLIT===" in full_text:
        parts = full_text.split("===SPLIT===")
        return parts[0].strip(), parts[1].strip()
    return full_text, "Place this exact product on a clean studio background, soft lighting, preserve the product shape and details, 8k"

# --- VISION через GEMINI ---
async def analyze_with_gemini(image_bytes: bytes, caption: str = "") -> tuple[str, str] | None:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    user_hint = f"\n\nПодсказка от пользователя: {caption}" if caption else ""
    full_prompt = VISION_PROMPT + user_hint

    payload = {
        "contents": [{
            "parts": [
                {"text": full_prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 3000}
    }

    async with aiohttp.ClientSession() as s:
        for model in GEMINI_MODELS:
            url = GEMINI_URL_TEMPLATE.format(model=model)
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
                        except (KeyError, IndexError):
                            continue
                        log.info("Gemini OK: %s", model)
                        return parse_vision_response(full_text)
                    elif resp.status in (503, 429, 404):
                        log.warning("Gemini %s -> %s, next model", model, resp.status)
                        continue
                    else:
                        err = await resp.text()
                        log.warning("Gemini %s HTTP %s: %s", model, resp.status, err[:200])
            except Exception as e:
                log.warning("Gemini %s error: %s", model, e)
    return None

async def analyze_photo_and_make_seo(image_bytes: bytes, caption: str = "") -> tuple[str, str]:
    result = await analyze_with_gemini(image_bytes, caption)
    if result:
        return result

    log.error("Gemini vision failed")
    return (
        "⚠️ Не удалось распознать товар на фото — сервис ИИ временно перегружен. "
        "Попробуйте через 1-2 минуты или добавьте короткую подпись к фото.",
        "Place this exact product on a clean studio background, soft lighting, preserve the product shape and details, 8k"
    )

# --- ГЕНЕРАЦИЯ ОБЛОЖКИ через REPLICATE FLUX KONTEXT PRO ---
async def generate_card_image(prompt: str, image_bytes: bytes | None = None) -> bytes | None:
    if not REPLICATE_API_TOKEN:
        log.error("REPLICATE_API_TOKEN not set")
        return None

    # Replicate требует URL или data URI. Передаём base64 data URI.
    input_data = {
        "prompt": prompt,
        "output_format": "jpg",
        "aspect_ratio": "3:4",
        "safety_tolerance": 2
    }

    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        input_data["input_image"] = f"data:image/jpeg;base64,{b64}"

    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
        # Заставляем Replicate ждать готовности до 60 сек (long-poll)
        "Prefer": "wait=60"
    }

    async with aiohttp.ClientSession() as s:
        for attempt in range(2):
            try:
                async with s.post(
                    REPLICATE_URL,
                    headers=headers,
                    json={"input": input_data},
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        status = data.get("status")
                        output = data.get("output")

                        # Если ещё processing — поллим вручную
                        if status in ("starting", "processing"):
                            poll_url = data.get("urls", {}).get("get")
                            if poll_url:
                                output = await _poll_replicate(s, poll_url, headers)

                        # output может быть строкой или списком
                        img_url = None
                        if isinstance(output, str):
                            img_url = output
                        elif isinstance(output, list) and output:
                            img_url = output[0]

                        if img_url:
                            async with s.get(img_url, timeout=aiohttp.ClientTimeout(total=60)) as img_resp:
                                if img_resp.status == 200:
                                    log.info("Replicate image OK")
                                    return await img_resp.read()
                        else:
                            log.warning("Replicate: no output. data=%s", str(data)[:300])
                    else:
                        err = await resp.text()
                        log.warning("Replicate HTTP %s: %s", resp.status, err[:300])
            except Exception as e:
                log.warning("Replicate attempt %d error: %s", attempt + 1, e)
            await asyncio.sleep(3)
    return None

async def _poll_replicate(s: aiohttp.ClientSession, poll_url: str, headers: dict) -> str | None:
    """Опрашивает Replicate пока не будет готово (макс 60 сек)."""
    for _ in range(20):
        await asyncio.sleep(3)
        try:
            async with s.get(poll_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    status = data.get("status")
                    if status == "succeeded":
                        out = data.get("output")
                        if isinstance(out, list) and out:
                            return out[0]
                        if isinstance(out, str):
                            return out
                        return None
                    if status in ("failed", "canceled"):
                        log.warning("Replicate poll: %s — %s", status, data.get("error"))
                        return None
        except Exception as e:
            log.warning("Poll error: %s", e)
    return None

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await get_user(message.from_user.id)
    await message.answer(
        "👋 Привет! Я ИИ-помощник для селлеров Wildberries и Ozon.\n\n"
        "📌 Что я умею:\n"
        "1️⃣ Отправь текстом название товара — соберу SEO-описание.\n"
        "2️⃣ Отправь фото товара (можно с подписью) — сделаю студийную обложку 3:4 "
        "с сохранением дизайна и напишу SEO-описание.\n\n"
        "✨ У тебя активен полный безлимит.",
        reply_markup=main_keyboard
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            (total,) = await cur.fetchone()
    await message.answer(f"📊 Админ-панель\n\nВсего пользователей: {total}")

# --- ФОТО ---
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    await get_user(user_id)

    wait_msg = await message.answer(
        "📸 Фото получено! Анализирую, пишу SEO и рисую обложку (30–90 сек)..."
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
                caption="🎨 Готовая студийная обложка для карточки (3:4)!"
            )
            await wait_msg.delete()
            await message.answer(seo_text, reply_markup=main_keyboard)
        else:
            await wait_msg.edit_text(
                f"⚠️ Не удалось сгенерировать обложку, но вот SEO-описание:\n\n{seo_text}"
            )
    except Exception as e:
        log.exception("handle_photo error")
        await wait_msg.edit_text(f"⚠️ Ошибка при обработке фото: {e}")

# --- ТЕКСТ И КНОПКИ ---
@dp.message(F.text)
async def handle_all_text(message: types.Message):
    text = message.text.strip()
    user_id = message.from_user.id

    if text == BTN_STATUS or "Статус системы" in text:
        await get_user(user_id)
        await message.answer(
            f"📊 Статус системы\n\n"
            f"🆔 ID: {user_id}\n"
            f"✨ Статус: 💎 PRO Безлимит\n"
            f"🖼 Генерация по фото: Активна (FLUX Kontext Pro)\n"
            f"🚀 Все системы в норме.",
            reply_markup=main_keyboard
        )
        return

    if text == BTN_PROFILE or "Мой профиль" in text:
        await get_user(user_id)
        await message.answer(
            f"👤 Профиль\n\n"
            f"• ID: {user_id}\n"
            f"• Тариф: 💎 PRO Безлимит\n"
            f"• Доступно: SEO + Генерация обложек (∞).",
            reply_markup=main_keyboard
        )
        return

    if text == BTN_GEN or "Сгенерировать описание" in text:
        await message.answer(
            "✍️ Отправь текст или фото товара — подготовлю SEO-описание и обложку.",
            reply_markup=main_keyboard
        )
        return

    if len(text) > 2000:
        await message.answer("Слишком длинный текст, сократи до 2000 символов.")
        return

    await get_user(user_id)
    wait_msg = await message.answer("⏳ Генерирую SEO-описание...")
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
