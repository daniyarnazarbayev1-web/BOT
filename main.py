import asyncio
import base64
from datetime import datetime
import io
import os
import random
import re
import socket
import threading
import time
from flask import Flask, render_template_string
import requests
from groq import Groq, RateLimitError
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ==================== КОНФИГУРАЦИЯ ИЗ ENVIRONMENT VARIABLES ====================
MODEL_NAME = "openai/gpt-oss-120b"
COOLDOWN_SECONDS = 12
MAX_HISTORY_LIMIT = 10  # Уменьшили лимит контекста для снижения нагрузок
last_message_time = 0

# Словарь для отслеживания времени последнего запроса КАЖДОГО пользователя
user_last_message_time = {}

SLEEP_DURATION = 10800 
is_sleeping = False
is_offline = False

HISTORY_FILE = "HISTORY.txt"

user_memory = {}
user_pending_queue = {}

SYSTEM_PROMPT = """Тебя зовут Кащей Якуб. Тебя создал Мокото Копырус.
Ты — реальный человек, обычный участник этого Telegram-чата.
ПРАВИЛА ОБЩЕНИЯ:
1. Отвечай прямо, коротко и завершено (1-2 предложения).
2. Твой стиль: остроумный, сухой, саркастичный и реалистичный.
3. Ты помнишь факты и детали о собеседнике из предыдущих сообщений.
4. СТРОГО ЗАПРЕЩЕНО использовать эмодзи, форматирование, спецсимволы и ссылки.
5. СТРОГО ЗАПРЕЩЕНО использовать символ @, юзернеймы, сервисный тон.
6. СТРОГО ЗАПРЕЩЕНО использовать или упоминать любые игровые и админ-команды (рулетка, дуэль, кубы, бан, кик, мут, варн, репорт, общий сбор, передать ириски).
7. Если прислали фото, коротко и остроумно прокомментируй его."""

# Чтение ключей напрямую из переменных окружения Render
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

app = Flask(__name__)

HTML_STATUS = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Статус Сервера</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #0f172a; color: #f8fafc; margin: 0; }
        .card { background: #1e293b; padding: 32px; border-radius: 16px; width: 360px; text-align: center; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3); }
        h2 { margin-top: 0; font-size: 1.5rem; color: #fff; }
        p { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; }
        .status-badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; background: #166534; color: #4ade80; border-radius: 9999px; font-weight: 600; font-size: 0.875rem; margin-top: 12px; }
        .dot { width: 8px; height: 8px; background-color: #22c55e; border-radius: 50%; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Userbot Dashboard</h2>
        <p>Сервер активен, Telegram Userbot запущен и принимает обновления.</p>
        <div class="status-badge">
            <span class="dot"></span> ONLINE
        </div>
    </div>
</body>
</html>
"""

groq_client = None
client = None
bot_loop = asyncio.new_event_loop()

@app.route("/", methods=["GET", "HEAD"])
def index():
    return render_template_string(HTML_STATUS), 200

# ----------------- REGEX ФИЛЬТРЫ БЕЗОПАСНОСТИ IRIS -----------------

# Опасные игровые и системные команды Iris (входные и выходные)
IRIS_DANGER_PATTERN = re.compile(
    r"([\.\/!─\+–\-]?\s*("
    r"рулетка|дуэль|кубы|мины|застрелиться|убиться|самоликвидация|"
    r"бан|кик|мут|варн|репорт|спам|снять|разжаловать|общий\s+сбор|созвать|"
    r"передать|голд|ириски|чек|алиасы|настройки"
    r"))", 
    re.IGNORECASE
)

def check_internet() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False

async def internet_monitor_loop():
    global is_offline
    while True:
        await asyncio.sleep(10)
        has_net = check_internet()
        if not has_net and not is_offline:
            is_offline = True
            print("[СЕТЬ] Интернет пропал.")
        elif has_net and is_offline:
            is_offline = False
            print("[СЕТЬ] Интернет появился.")

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'\S*@\S*', '', text)  # Удаление юзернеймов/почты
    return re.sub(r'\s+', ' ', text).strip()

def process_generated_text(text: str) -> str:
    """Жесткий фильтр сгенерированного ответа ИИ: предотвращает кик и блокировку"""
    if not text:
        return ""
    
    # 1. Проверка на ссылки
    has_url = bool(re.search(r'(https?://\S+|www\.\S+|\bt\.me/\S+|\b[a-zA-Z0-9.-]+\.(?:ru|com|net|org|io|me)\b)', text, re.IGNORECASE))
    
    # 2. Проверка на опасные команды Iris
    has_iris_danger = bool(IRIS_DANGER_PATTERN.search(text))
    
    # 3. Если ИИ попытался выдать команду или ссылку — отменяем отправку полностью
    if has_url or has_iris_danger:
        print(f"[БЛОКИРОВКА ИИ] Отфильтровано опасное сообщение: {text}")
        return ""
        
    return clean_text(text)

def encode_image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode('utf-8')

def generate_image_pollinations(prompt: str) -> bytes | None:
    try:
        clean_prompt = re.sub(r'^(нарисуй|сгенерируй|покажи как выглядит|нарисуй фото)\s*', '', prompt, flags=re.IGNORECASE)
        url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(clean_prompt)}?width=800&height=800&nologo=true"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            return response.content
    except Exception as e:
        print(f"Ошибка генерации картинки: {e}")
    return None

async def trigger_sleep_mode():
    global is_sleeping
    if is_sleeping:
        return

    is_sleeping = True
    print("[ЛИМИТЫ] Сон на 3 часа.")
    await asyncio.sleep(SLEEP_DURATION)
    is_sleeping = False

def analyze_image_with_qwen(image_bytes: bytes, caption_text: str = "") -> tuple[str, bool]:
    if not groq_client:
        return "", False

    base64_image = encode_image_to_base64(image_bytes)
    user_content = [
        {"type": "text", "text": caption_text if caption_text else "Прокомментируй это изображение коротко и естественно."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
    ]

    try:
        completion = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content}
            ],
            temperature=0.6,
            max_tokens=150,
        )
        return process_generated_text(completion.choices[0].message.content or ""), False
    except RateLimitError:
        return "", True
    except Exception as e:
        print(f"Ошибка анализа изображения: {e}")
        return "", False

def split_into_chunks(text: str) -> list[str]:
    if len(text) < 90:
        return [text]

    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = ""

    for sentence in sentences:
        if not sentence:
            continue
        if len(current_chunk) + len(sentence) < 100:
            current_chunk = (current_chunk + " " + sentence).strip()
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [text]

def cleanup_inactive_users():
    current_time = time.time()
    expired_users = [uid for uid, data in user_memory.items() if current_time - data["last_active"] > 3600]
    for uid in expired_users:
        del user_memory[uid]

def log_to_file(user_id: int, username: str, role: str, text: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [User ID: {user_id} | @{username or 'no_username'}] {role.upper()}: {text}\n"
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception:
        pass

def get_ai_response(user_id: int, combined_text: str) -> tuple[str, bool]:
    if not groq_client:
        return "", False

    cleanup_inactive_users()

    if user_id not in user_memory:
        user_memory[user_id] = {"last_active": time.time(), "messages": []}

    user_memory[user_id]["last_active"] = time.time()
    history = user_memory[user_id]["messages"]

    temp_history = history + [{"role": "user", "content": combined_text}]
    
    if len(temp_history) > MAX_HISTORY_LIMIT:
        temp_history = temp_history[-MAX_HISTORY_LIMIT:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + temp_history

    try:
        completion = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.6,
            max_tokens=150,
        )
        raw_reply = completion.choices[0].message.content or ""
        final_reply = process_generated_text(raw_reply)

        if final_reply:
            history.append({"role": "user", "content": combined_text})
            history.append({"role": "assistant", "content": final_reply})
            
            user_memory[user_id]["messages"] = history[-MAX_HISTORY_LIMIT:]
            return final_reply, False

    except RateLimitError:
        return "", True
    except Exception as e:
        print(f"Ошибка запроса к API Groq: {e}")
        return "", False

    return "", False

async def send_split_messages(event, full_text: str):
    chunks = split_into_chunks(full_text)
    for i, chunk in enumerate(chunks):
        async with event.client.action(event.chat_id, "typing"):
            typing_delay = max(1.5, min(len(chunk) * 0.05 + random.uniform(0.3, 1.0), 4.0))
            await asyncio.sleep(typing_delay)
            if i == 0:
                await event.reply(chunk)
            else:
                await event.client.send_message(event.chat_id, chunk)

async def autonomous_chat_initiator():
    """Редкая активация бота (раз в 3-6 часов)"""
    while True:
        await asyncio.sleep(random.randint(10800, 21600))
        if is_sleeping or is_offline or not client or not CHAT_ID:
            continue
        try:
            messages = await client.get_messages(CHAT_ID, limit=5)
            chat_context = [f"Участник: {msg.text}" for msg in reversed(messages) if msg.text and not IRIS_DANGER_PATTERN.search(msg.text)]
            if not chat_context:
                continue

            full_context = "ПОСЛЕДНИЕ СООБЩЕНИЯ В ЧАТЕ:\n" + "\n".join(chat_context) + "\n\nНапиши короткую естественную реплику в тему этого разговора."
            ai_reply, is_rate_limit = get_ai_response(0, full_context)
            if is_rate_limit:
                asyncio.create_task(trigger_sleep_mode())
                continue

            if ai_reply:
                chunks = split_into_chunks(ai_reply)
                for chunk in chunks:
                    async with client.action(CHAT_ID, "typing"):
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                        await client.send_message(CHAT_ID, chunk)
                log_to_file(0, "autonomous", "Bot_Initiative", ai_reply)
        except Exception as e:
            print(f"Ошибка фонового цикла: {e}")

def register_telegram_handlers():
    if not client or not CHAT_ID:
        return

    @client.on(events.NewMessage(chats=CHAT_ID))
    async def handle_message(event):
        global last_message_time, user_last_message_time
        if event.out or is_sleeping or is_offline:
            return

        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return

        user_id = sender.id
        username = getattr(sender, "username", "") or ""
        text = event.text or ""
        has_photo = bool(event.photo)

        # 1. ЗАЩИТА ОТ АБУЗА: Полный игнор любых провокаций Iris и системных команд
        if IRIS_DANGER_PATTERN.search(text):
            print(f"[ОБХОД АБУЗА] Игнорируем опасную команду от user_{user_id}: {text}")
            return

        if not text and not has_photo:
            return

        log_to_file(user_id, username, "User", text or "[PHOTO]")
        me = await client.get_me()

        is_reply_to_me = False
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.sender_id == me.id:
                is_reply_to_me = True

        is_mentioned = me.username and f"@{me.username}" in text
        
        # 2. ОГРАНИЧЕНИЕ ВЛЕЗАНИЯ: Личное обращение — 100%, случайное влезание — 3%
        should_respond = is_reply_to_me or is_mentioned or (random.random() < 0.03)

        if not should_respond:
            return

        current_time = time.time()

        # 3. КУЛДАУН: Игнорируем слишком частые запросы от одного юзера без флуда в ответ
        last_user_time = user_last_message_time.get(user_id, 0)
        if current_time - last_user_time < COOLDOWN_SECONDS:
            return

        # Задержка реакции для естественности
        await asyncio.sleep(random.uniform(1.8, 3.5))

        if current_time - last_message_time >= COOLDOWN_SECONDS or is_reply_to_me or is_mentioned:
            user_last_message_time[user_id] = current_time
            last_message_time = time.time()

            # Обработка генерации картинок
            if text and any(k in text.lower() for k in ["нарисуй", "сгенерируй", "покажи как выглядит"]):
                async with event.client.action(event.chat_id, "photo"):
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    img_data = generate_image_pollinations(text)
                    if img_data:
                        photo_file = io.BytesIO(img_data)
                        photo_file.name = "photo.jpg"
                        await event.reply(file=photo_file)
                        log_to_file(user_id, username, "Bot", "[GENERATED_IMAGE]")
                        return

            # Обработка входящих фото
            if has_photo:
                async with event.client.action(event.chat_id, "typing"):
                    photo_bytes = await event.download_media(file=bytes)
                    ai_reply, is_rate_limit = analyze_image_with_qwen(photo_bytes, caption_text=text)
                    if is_rate_limit:
                        asyncio.create_task(trigger_sleep_mode())
                        return
                    if ai_reply:
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                        await event.reply(ai_reply)
                        log_to_file(user_id, username, "Bot", ai_reply)
                        return

            # Текстовый ответ
            if user_id not in user_pending_queue:
                user_pending_queue[user_id] = []
            user_pending_queue[user_id].append(text)

            pending_messages = user_pending_queue.pop(user_id, [text])
            combined_text = "\n".join(pending_messages)

            ai_reply, is_rate_limit = get_ai_response(user_id, combined_text)
            if is_rate_limit:
                asyncio.create_task(trigger_sleep_mode())
                return
            if ai_reply:
                log_to_file(user_id, username, "Bot", ai_reply)
                await send_split_messages(event, ai_reply)

# ----------------- СТАРТ И ИНИЦИАЛИЗАЦИЯ -----------------

def start_bot_tasks():
    bot_loop.create_task(autonomous_chat_initiator())
    bot_loop.create_task(internet_monitor_loop())

def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

async def main():
    global client, groq_client

    if GROQ_API_KEY:
        groq_client = Groq(api_key=GROQ_API_KEY)

    if API_ID and API_HASH and SESSION_STRING:
        print("[ИНФО] Подключение юзербота через StringSession...")
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, loop=bot_loop)
        await client.connect()
        
        if await client.is_user_authorized():
            print("[УСПЕХ] Юзербот успешно авторизован!")
            register_telegram_handlers()
            start_bot_tasks()
        else:
            print("[ОШИБКА] Указанная SESSION_STRING недействительна.")
    else:
        print("[ВНИМАНИЕ] Переменные окружения не заполнены!")

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.set_event_loop(bot_loop)
    try:
        bot_loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
