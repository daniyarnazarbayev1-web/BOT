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
from flask import Flask, request, render_template_string
import requests
from groq import Groq, RateLimitError
from telethon import TelegramClient, events

# ==================== КОНФИГУРАЦИЯ БЕЗ ТОКЕНОВ ====================
# Токены берутся из переменных окружения (если есть), либо вводятся через веб-сайт.
# В самом коде секретные ключи НЕ хранятся.
# ==================================================================

MODEL_NAME = "qwen/qwen3.8-27b"
COOLDOWN_SECONDS = 10
last_message_time = 0

SLEEP_DURATION = 10800 
is_sleeping = False
is_offline = False

HISTORY_FILE = "HISTORY.txt"
SESSION_FILE = "user_session.session"

user_memory = {}
user_pending_queue = {}

SYSTEM_PROMPT = """Ты — реальный человек, участник этого Telegram-чата.
ПРАВИЛА ОБЩЕНИЯ:
1. Отвечай всегда прямо и завершенно. Никаких обрывок, недосказанностей и технических пометок.
2. Общайся естественно, без шаблонов, формализма, сервисного тона ("чем помочь", "пиши если что") и заученных фраз.
3. К КАЖДОМУ пользователю у тебя формируется СВОЕ уникальное отношение на основе его сообщений.
4. Пиши живым языком обычного человека: коротко и емко (1-3 предложения). Не пиши длинных пластов текста.
5. Если тебе прислали фото, коротко прокомментируй его в обычном разговорном стиле.
6. Ты НЕ бот и НЕ ИИ.
7. СТРОГО ЗАПРЕЩЕНО использовать символ @ и юзернеймы."""

# Хранилище настроек и состояния авторизации в памяти
config_data = {
    "api_id": int(os.getenv("API_ID", "0")),
    "api_hash": os.getenv("API_HASH", ""),
    "groq_key": os.getenv("GROQ_API_KEY", ""),
    "chat_id": int(os.getenv("CHAT_ID", "0")),
    "configured": False
}

login_data = {}

# ----------------- ВЕБ-СЕРВЕР Flask -----------------
app = Flask(__name__)

HTML_FORM = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Настройка и Авторизация Telegram</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #121212; color: #e0e0e0; margin: 0; }
        .card { background: #1e1e1e; padding: 30px; border-radius: 12px; width: 380px; text-align: center; box-shadow: 0 8px 24px rgba(0,0,0,0.6); }
        h2 { margin-top: 0; color: #fff; }
        h3 { color: #aaa; font-weight: normal; margin-bottom: 20px; }
        label { display: block; text-align: left; font-size: 12px; margin-top: 10px; color: #bbb; }
        input, button { width: 100%; padding: 12px; margin: 6px 0 12px 0; box-sizing: border-box; border-radius: 6px; border: 1px solid #333; background: #2a2a2a; color: white; font-size: 14px; }
        input:focus { border-color: #0088cc; outline: none; }
        button { background: #0088cc; color: white; font-weight: bold; cursor: pointer; border: none; margin-top: 15px; transition: background 0.2s; }
        button:hover { background: #0077b3; }
        .status { background: #1b3a24; color: #4CAF50; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        .error { background: #3a1b1b; color: #f44336; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Панель управления</h2>
        
        {% if status %}
            <div class="status">{{ status }}</div>
        {% endif %}
        {% if error %}
            <div class="error">{{ error }}</div>
        {% endif %}
        
        {% if step == 'tokens' %}
        <h3>Шаг 1: Ввод ключей и токенов</h3>
        <form method="POST" action="/save_tokens">
            <label>API ID (Telegram):</label>
            <input type="number" name="api_id" placeholder="34787451" value="{{ config.api_id if config.api_id else '' }}" required>
            
            <label>API Hash (Telegram):</label>
            <input type="text" name="api_hash" placeholder="840b6dfd030f..." value="{{ config.api_hash }}" required>
            
            <label>Groq API Key:</label>
            <input type="password" name="groq_key" placeholder="gsk_..." value="{{ config.groq_key }}" required>
            
            <label>Chat ID Telegram:</label>
            <input type="number" name="chat_id" placeholder="-1002094465456" value="{{ config.chat_id if config.chat_id else '' }}" required>
            
            <button type="submit">Сохранить и продолжить</button>
        </form>
        
        {% elif step == 'phone' %}
        <h3>Шаг 2: Ввод номера телефона</h3>
        <form method="POST" action="/send_phone">
            <label>Номер телефона Telegram:</label>
            <input type="text" name="phone" placeholder="+79991112233" required>
            <button type="submit">Запросить код подтверждения</button>
        </form>
        
        {% elif step == 'code' %}
        <h3>Шаг 3: Авторизация</h3>
        <form method="POST" action="/send_code">
            <label>Код из Telegram:</label>
            <input type="text" name="code" placeholder="12345" required>
            
            <label>Облачный пароль 2FA (если есть):</label>
            <input type="password" name="password" placeholder="Оставьте пустым, если нет">
            
            <button type="submit">Войти и запустить бота</button>
        </form>
        
        {% elif step == 'done' %}
        <h3>Успешно!</h3>
        <p style="color: #bbb; font-size: 14px;">Бот успешно авторизован, токены применены. Сессия активна.</p>
        {% endif %}
    </div>
</body>
</html>
"""

groq_client = None
client = None
bot_loop = asyncio.new_event_loop()

def init_clients():
    global groq_client, client
    if config_data["groq_key"]:
        groq_client = Groq(api_key=config_data["groq_key"])
    if config_data["api_id"] and config_data["api_hash"]:
        client = TelegramClient("user_session", config_data["api_id"], config_data["api_hash"])

@app.route("/")
def index():
    if os.path.exists(SESSION_FILE) and config_data["configured"]:
        return render_template_string(HTML_FORM, step='done', status="Бот запущен и работает!", config=config_data)
    
    if not config_data["configured"]:
        return render_template_string(HTML_FORM, step='tokens', status="", config=config_data)
        
    step = 'code' if 'phone_code_hash' in login_data else 'phone'
    return render_template_string(HTML_FORM, step=step, status="", config=config_data)

@app.route("/save_tokens", methods=["POST"])
def save_tokens():
    try:
        config_data["api_id"] = int(request.form.get("api_id", 0))
        config_data["api_hash"] = request.form.get("api_hash", "").strip()
        config_data["groq_key"] = request.form.get("groq_key", "").strip()
        config_data["chat_id"] = int(request.form.get("chat_id", 0))
        
        config_data["configured"] = True
        init_clients()
        
        return render_template_string(HTML_FORM, step='phone', status="Токены успешно сохранены. Введите номер телефона.", config=config_data)
    except Exception as e:
        return render_template_string(HTML_FORM, step='tokens', error=f"Ошибка в данных: {e}", config=config_data)

@app.route("/send_phone", methods=["POST"])
def send_phone():
    phone = request.form.get("phone", "").strip()
    login_data['phone'] = phone
    
    async def _send():
        await client.connect()
        res = await client.send_code_request(phone)
        login_data['phone_code_hash'] = res.phone_code_hash

    try:
        asyncio.run_coroutine_threadsafe(_send(), bot_loop).result()
        return render_template_string(HTML_FORM, step='code', status="Код подтверждения отправлен в Telegram!", config=config_data)
    except Exception as e:
        return render_template_string(HTML_FORM, step='phone', error=f"Ошибка отправки кода: {e}", config=config_data)

@app.route("/send_code", methods=["POST"])
def send_code():
    code = request.form.get("code", "").strip()
    password = request.form.get("password", "").strip()

    async def _login():
        try:
            await client.sign_in(phone=login_data['phone'], code=code, phone_code_hash=login_data.get('phone_code_hash'))
        except Exception as e:
            if "Two-steps verification" in str(e) and password:
                await client.sign_in(password=password)
            else:
                raise e

    try:
        asyncio.run_coroutine_threadsafe(_login(), bot_loop).result()
        register_telegram_handlers()
        start_bot_tasks()
        return render_template_string(HTML_FORM, step='done', status="Авторизация прошла успешно! Бот запущен.", config=config_data)
    except Exception as e:
        return render_template_string(HTML_FORM, step='code', error=f"Ошибка входа: {e}", config=config_data)

def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ----------------- ОСНОВНАЯ ЛОГИКА БОТА -----------------
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
            try:
                await client.send_message(config_data["chat_id"], "всем привет, у меня инета не было")
            except Exception as e:
                print(f"Ошибка отправки: {e}")

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'\S*@\S*', '', text)
    return re.sub(r'\s+', ' ', text).strip()

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
    try:
        await client.send_message(config_data["chat_id"], "Чет спать хочу, я спать, всем пока.")
    except Exception as e:
        print(f"Ошибка отправки: {e}")

    await asyncio.sleep(SLEEP_DURATION)

    try:
        await client.send_message(config_data["chat_id"], "ух.. хорошо поспал.. всем привет.")
    except Exception as e:
        print(f"Ошибка отправки: {e}")

    is_sleeping = False

def analyze_image_with_qwen(image_bytes: bytes, caption_text: str = "") -> tuple[str, bool]:
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
            temperature=0.7,
            max_tokens=250,
        )
        return clean_text(completion.choices[0].message.content or ""), False
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
    expired_users = [uid for uid, data in user_memory.items() if current_time - data["last_active"] > 7200]
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
    cleanup_inactive_users()

    if user_id not in user_memory:
        user_memory[user_id] = {"last_active": time.time(), "messages": []}

    user_memory[user_id]["last_active"] = time.time()
    history = user_memory[user_id]["messages"]

    temp_history = history + [{"role": "user", "content": combined_text}]
    if len(temp_history) > 10:
        temp_history = temp_history[-10:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + temp_history

    try:
        completion = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.7,
            max_tokens=250,
        )
        raw_reply = completion.choices[0].message.content or ""
        final_reply = clean_text(raw_reply)

        if final_reply:
            history.append({"role": "user", "content": combined_text})
            history.append({"role": "assistant", "content": final_reply})
            user_memory[user_id]["messages"] = history[-10:]
            return final_reply, False

    except RateLimitError:
        return "", True
    except Exception as e:
        print(f"Ошибка запроса к API: {e}")
        return "", False

    return "", False

async def send_split_messages(event, full_text: str):
    chunks = split_into_chunks(full_text)
    for i, chunk in enumerate(chunks):
        async with event.client.action(event.chat_id, "typing"):
            typing_delay = max(1.8, min(len(chunk) * 0.06 + random.uniform(0.5, 1.5), 6.0))
            await asyncio.sleep(typing_delay)
            if i == 0:
                await event.reply(chunk)
            else:
                await event.client.send_message(event.chat_id, chunk)

async def autonomous_chat_initiator():
    await client.wait_until_ready()
    while True:
        await asyncio.sleep(random.randint(1800, 3600))
        if is_sleeping or is_offline:
            continue
        try:
            messages = await client.get_messages(config_data["chat_id"], limit=5)
            chat_context = [f"Участник: {msg.text}" for msg in reversed(messages) if msg.text]
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
                    async with client.action(config_data["chat_id"], "typing"):
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                        await client.send_message(config_data["chat_id"], chunk)
                log_to_file(0, "autonomous", "Bot_Initiative", ai_reply)
        except Exception as e:
            print(f"Ошибка фонового цикла: {e}")

def register_telegram_handlers():
    @client.on(events.NewMessage(chats=config_data["chat_id"]))
    async def handle_message(event):
        global last_message_time
        if event.out or is_sleeping or is_offline:
            return

        sender = await event.get_sender()
        if not sender:
            return

        user_id = sender.id
        username = getattr(sender, "username", "") or ""
        text = event.text or ""
        has_photo = bool(event.photo)

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
        should_respond = is_reply_to_me or is_mentioned or (random.random() < 0.12)

        await asyncio.sleep(2.5)
        current_time = time.time()

        if should_respond and (current_time - last_message_time >= COOLDOWN_SECONDS or is_reply_to_me or is_mentioned):
            last_message_time = time.time()

            if text and any(k in text.lower() for k in ["нарисуй", "сгенерируй", "покажи как выглядит", "нарисуй фото"]):
                async with event.client.action(event.chat_id, "photo"):
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    img_data = generate_image_pollinations(text)
                    if img_data:
                        photo_file = io.BytesIO(img_data)
                        photo_file.name = "photo.jpg"
                        await event.reply(file=photo_file)
                        log_to_file(user_id, username, "Bot", "[GENERATED_IMAGE]")
                        return

            if has_photo:
                async with event.client.action(event.chat_id, "typing"):
                    photo_bytes = await event.download_media(file=bytes)
                    ai_reply, is_rate_limit = analyze_image_with_qwen(photo_bytes, caption_text=text)
                    if is_rate_limit:
                        asyncio.create_task(trigger_sleep_mode())
                        return
                    if ai_reply:
                        await asyncio.sleep(random.uniform(1.5, 3.5))
                        await event.reply(ai_reply)
                        log_to_file(user_id, username, "Bot", ai_reply)
                        return

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

# ----------------- ЗАПУСК -----------------
def start_bot_tasks():
    bot_loop.create_task(autonomous_chat_initiator())
    bot_loop.create_task(internet_monitor_loop())

def run_telethon():
    asyncio.set_event_loop(bot_loop)
    if config_data["api_id"] and config_data["api_hash"]:
        init_clients()
        bot_loop.run_until_complete(client.connect())
        if bot_loop.run_until_complete(client.is_user_authorized()):
            print("Сессия найдена. Бот запускается...")
            config_data["configured"] = True
            register_telegram_handlers()
            start_bot_tasks()
    bot_loop.run_forever()

threading.Thread(target=run_telethon, daemon=True).start()
run_flask()
