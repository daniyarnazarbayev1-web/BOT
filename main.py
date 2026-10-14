import asyncio
import base64
import json
import io
import os
import random
import re
import socket
import threading
import time
import atexit
from datetime import datetime
from flask import Flask, render_template_string, jsonify
import requests
from groq import Groq, RateLimitError
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

# ==================== КОНФИГУРАЦИЯ ИЗ ENVIRONMENT VARIABLES ====================

MODEL_NAME = os.getenv("MODEL_NAME", "openai/gpt-oss-120b")
VISION_MODEL_NAME = os.getenv("VISION_MODEL_NAME", MODEL_NAME)

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "12"))
MAX_HISTORY_LIMIT = int(os.getenv("MAX_HISTORY_LIMIT", "14"))
MAX_TOKENS_TEXT = int(os.getenv("MAX_TOKENS_TEXT", "350"))
MAX_TOKENS_VISION = int(os.getenv("MAX_TOKENS_VISION", "250"))

RANDOM_REPLY_CHANCE = float(os.getenv("RANDOM_REPLY_CHANCE", "0.03"))
NIGHT_REPLY_MULTIPLIER = float(os.getenv("NIGHT_REPLY_MULTIPLIER", "0.3"))
REACTION_CHANCE = float(os.getenv("REACTION_CHANCE", "0.04"))

SLEEP_DURATION = int(os.getenv("SLEEP_DURATION", "10800"))
MEMORY_FILE = os.getenv("MEMORY_FILE", "memory.json")
HISTORY_FILE = os.getenv("HISTORY_FILE", "HISTORY.txt")

is_sleeping = False
is_offline = False
last_message_time = 0
user_last_message_time = {}
user_pending_queue = {}

REACTION_EMOJIS = ["👍", "😁", "🔥", "😐", "🤔", "😏", "👀", "😂"]

MOOD_LIST = [
    "саркастичный и слегка раздражённый",
    "уставший и немногословный",
    "философски настроенный",
    "циничный и язвительный",
    "игривый, но с ленцой",
    "подозрительный ко всем вокруг",
]
current_mood = random.choice(MOOD_LIST)

STATS = {
    "start_time": time.time(),
    "messages_seen": 0,
    "replies_sent": 0,
    "images_generated": 0,
    "rate_limit_hits": 0,
}

SYSTEM_PROMPT_BASE = """Тебя зовут Кащей Якуб. Тебя создал Мокото Копырус.
Ты — реальный человек, обычный участник этого Telegram-чата.
ПРАВИЛА ОБЩЕНИЯ:

1. Отвечай прямо, коротко и ЗАВЕРШЕННО мыслью (1-3 предложения). Никогда не обрывай фразу на полуслове — если мысль не помещается, сократи её до одного законченного предложения.
2. Твой стиль: остроумный, сухой, саркастичный и реалистичный.
3. Ты помнишь факты и детали о собеседнике из предыдущих сообщений.
4. СТРОГО ЗАПРЕЩЕНО использовать эмодзи, форматирование, спецсимволы и ссылки.
5. СТРОГО ЗАПРЕЩЕНО использовать символ @, юзернеймы, сервисный тон.
6. СТРОГО ЗАПРЕЩЕНО использовать или упоминать любые игровые и админ-команды (рулетка, дуэль, кубы, бан, кик, мут, варн, репорт, общий сбор, передать ириски).
7. Если прислали фото, коротко и остроумно прокомментируй его.
8. Каждый ответ должен быть законченной мыслью с точкой, вопросом или восклицанием в конце."""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT_BASE + f"\nТвоё текущее настроение сейчас: {current_mood}."


# Чтение ключей напрямую из переменных окружения Render
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Поддержка нескольких ключей Groq через запятую для ротации при лимитах
_raw_keys = os.getenv("GROQ_API_KEY", "").strip()
GROQ_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_groq_key_index = 0

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
        .card { background: #1e293b; padding: 32px; border-radius: 16px; width: 380px; text-align: center; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3); }
        h2 { margin-top: 0; font-size: 1.5rem; color: #fff; }
        p { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; }
        .status-badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; background: #166534; color: #4ade80; border-radius: 9999px; font-weight: 600; font-size: 0.875rem; margin-top: 12px; }
        .status-badge.off { background: #7f1d1d; color: #fca5a5; }
        .dot { width: 8px; height: 8px; background-color: #22c55e; border-radius: 50%; }
        .stats { margin-top: 20px; text-align: left; font-size: 0.85rem; color: #cbd5e1; background:#0f172a; padding:12px; border-radius:10px; }
        .stats div { display:flex; justify-content:space-between; padding: 3px 0; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Userbot Dashboard</h2>
        <p>Сервер активен, Telegram Userbot запущен и принимает обновления.</p>
        <div class="status-badge {{ 'off' if sleeping_or_offline else '' }}">
            <span class="dot"></span> {{ 'SLEEPING/OFFLINE' if sleeping_or_offline else 'ONLINE' }}
        </div>
        <div class="stats">
            <div><span>Настроение:</span><span>{{ mood }}</span></div>
            <div><span>Сообщений увидено:</span><span>{{ seen }}</span></div>
            <div><span>Ответов отправлено:</span><span>{{ sent }}</span></div>
            <div><span>Картинок сгенерировано:</span><span>{{ images }}</span></div>
            <div><span>Аптайм:</span><span>{{ uptime }}</span></div>
        </div>
    </div>
</body>
</html>
"""

groq_client = None
client = None
bot_loop = asyncio.new_event_loop()


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}ч {m}м {s}с"


@app.route("/", methods=["GET", "HEAD"])
def index():
    return render_template_string(
        HTML_STATUS,
        sleeping_or_offline=is_sleeping or is_offline,
        mood=current_mood,
        seen=STATS["messages_seen"],
        sent=STATS["replies_sent"],
        images=STATS["images_generated"],
        uptime=_format_uptime(time.time() - STATS["start_time"]),
    ), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "offline" if is_offline else ("sleeping" if is_sleeping else "online"),
        "mood": current_mood,
    }), 200


@app.route("/stats", methods=["GET"])
def stats_endpoint():
    data = dict(STATS)
    data["uptime_seconds"] = time.time() - STATS["start_time"]
    data["active_users"] = len(user_memory)
    return jsonify(data), 200


# ----------------- REGEX ФИЛЬТРЫ БЕЗОПАСНОСТИ IRIS -----------------

IRIS_DANGER_PATTERN = re.compile(
    r"([./!─+–-]?\s*("
    r"рулетка|дуэль|кубы|мины|застрелиться|убиться|самоликвидация|"
    r"бан|кик|мут|варн|репорт|спам|снять|разжаловать|общий\s+сбор|созвать|"
    r"передать|голд|ириски|чек|алиасы|настройки"
    r"))",
    re.IGNORECASE
)

# ----------------- ПАМЯТЬ (с персистентностью на диск) -----------------

user_memory = {}
_memory_lock = threading.Lock()


def load_memory():
    global user_memory
    if not os.path.exists(MEMORY_FILE):
        return
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        user_memory = {int(k): v for k, v in raw.items()}
        print(f"[ПАМЯТЬ] Загружено {len(user_memory)} профилей пользователей.")
    except Exception as e:
        print(f"[ПАМЯТЬ] Не удалось загрузить память: {e}")


def save_memory():
    with _memory_lock:
        try:
            tmp_path = MEMORY_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(user_memory, f, ensure_ascii=False)
            os.replace(tmp_path, MEMORY_FILE)
        except Exception as e:
            print(f"[ПАМЯТЬ] Ошибка сохранения: {e}")


def cleanup_inactive_users():
    current_time = time.time()
    expired_users = [uid for uid, data in user_memory.items() if current_time - data.get("last_active", 0) > 3600 * 6]
    for uid in expired_users:
        del user_memory[uid]


# ----------------- СЕТЬ -----------------

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


# ----------------- ОБРАБОТКА ТЕКСТА -----------------

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'\S*@\S*', '', text)  # Удаление юзернеймов/почты
    return re.sub(r'\s+', ' ', text).strip()


def cut_to_last_full_sentence(text: str) -> str:
    """Если ответ был обрезан моделью по лимиту токенов — аккуратно
    дорезаем его до последнего законченного предложения, а если таковых
    нет — до последнего целого слова, чтобы никогда не рвать слово пополам."""
    if not text:
        return text
    sentence_ends = [m.end() for m in re.finditer(r'[.!?…]+(?=\s|$)', text)]
    if sentence_ends:
        return text[:sentence_ends[-1]].strip()
    if " " in text:
        return text.rsplit(" ", 1)[0].strip()
    return text.strip()


def process_generated_text(text: str, finish_reason: str = None) -> str:
    """Жесткий фильтр сгенерированного ответа ИИ: предотвращает кик, блокировку
    и обрезание слов на полуслове."""
    if not text:
        return ""

    text = clean_text(text)

    # Если модель упёрлась в лимит токенов — дорезаем до целого предложения/слова
    if finish_reason == "length":
        text = cut_to_last_full_sentence(text)

    if not text:
        return ""

    # Проверка на ссылки
    has_url = bool(re.search(r'(https?://\S+|www\.\S+|\bt\.me/\S+|\b[a-zA-Z0-9.-]+\.(?:ru|com|net|org|io|me)\b)', text, re.IGNORECASE))

    # Проверка на опасные команды Iris
    has_iris_danger = bool(IRIS_DANGER_PATTERN.search(text))

    if has_url or has_iris_danger:
        print(f"[БЛОКИРОВКА ИИ] Отфильтровано опасное сообщение: {text}")
        return ""

    return text


def split_into_chunks(text: str) -> list:
    """Делит текст на части по границам предложений/слов, никогда не разрывая слово."""
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


def encode_image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode('utf-8')


def generate_image_pollinations(prompt: str) -> bytes:
    try:
        clean_prompt = re.sub(r'^(нарисуй|сгенерируй|покажи как выглядит|нарисуй фото)\s*', '', prompt, flags=re.IGNORECASE)
        url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(clean_prompt)}?width=800&height=800&nologo=true"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            return response.content
    except Exception as e:
        print(f"Ошибка генерации картинки: {e}")
    return None


def log_to_file(user_id: int, username: str, role: str, text: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [User ID: {user_id} | @{username or 'no_username'}] {role.upper()}: {text}\n"
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception:
        pass


# ----------------- GROQ С РОТАЦИЕЙ КЛЮЧЕЙ -----------------

def init_groq_client():
    global groq_client
    if GROQ_API_KEYS:
        groq_client = Groq(api_key=GROQ_API_KEYS[_groq_key_index])


def rotate_groq_key() -> bool:
    """Переключается на следующий доступный ключ Groq. Возвращает True, если удалось переключиться."""
    global _groq_key_index
    if len(GROQ_API_KEYS) <= 1:
        return False
    _groq_key_index = (_groq_key_index + 1) % len(GROQ_API_KEYS)
    init_groq_client()
    print(f"[GROQ] Переключение на ключ #{_groq_key_index + 1}")
    return True


async def trigger_sleep_mode():
    global is_sleeping
    if is_sleeping:
        return
    is_sleeping = True
    STATS["rate_limit_hits"] += 1
    print("[ЛИМИТЫ] Сон на 3 часа.")
    await asyncio.sleep(SLEEP_DURATION)
    is_sleeping = False


def _chat_completion_with_retry(messages, max_tokens, temperature=0.6):
    """Вызывает Groq, при RateLimitError пробует ротацию ключей перед тем как сдаться."""
    attempts = max(1, len(GROQ_API_KEYS))
    last_exception = None
    for _ in range(attempts):
        try:
            completion = groq_client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            choice = completion.choices[0]
            return choice.message.content or "", choice.finish_reason, False
        except RateLimitError as e:
            last_exception = e
            if not rotate_groq_key():
                return "", None, True
        except Exception as e:
            print(f"Ошибка запроса к API Groq: {e}")
            return "", None, False
    if last_exception:
        return "", None, True
    return "", None, False


def analyze_image_with_ai(image_bytes: bytes, caption_text: str = "") -> tuple:
    if not groq_client:
        return "", False

    base64_image = encode_image_to_base64(image_bytes)
    user_content = [
        {"type": "text", "text": caption_text if caption_text else "Прокомментируй это изображение коротко и естественно."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
    ]

    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": user_content}
    ]

    raw_reply, finish_reason, is_rate_limit = _chat_completion_with_retry(messages, MAX_TOKENS_VISION)
    if is_rate_limit:
        return "", True
    return process_generated_text(raw_reply, finish_reason), False


def get_ai_response(user_id: int, combined_text: str) -> tuple:
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

    messages = [{"role": "system", "content": build_system_prompt()}] + temp_history

    raw_reply, finish_reason, is_rate_limit = _chat_completion_with_retry(messages, MAX_TOKENS_TEXT)
    if is_rate_limit:
        return "", True

    final_reply = process_generated_text(raw_reply, finish_reason)

    if final_reply:
        history.append({"role": "user", "content": combined_text})
        history.append({"role": "assistant", "content": final_reply})
        user_memory[user_id]["messages"] = history[-MAX_HISTORY_LIMIT:]
        save_memory()
        return final_reply, False

    return "", False


def summarize_old_history(user_id: int):
    """Если история слишком длинная — сжимает старую часть в краткое резюме через ИИ,
    чтобы не терять контекст о человеке даже после обрезки сообщений."""
    if not groq_client or user_id not in user_memory:
        return
    history = user_memory[user_id]["messages"]
    if len(history) < MAX_HISTORY_LIMIT:
        return
    try:
        text_dump = "\n".join(f"{m['role']}: {m['content']}" for m in history[:-4])
        messages = [
            {"role": "system", "content": "Сожми следующий диалог в 2-3 коротких факта о собеседнике для памяти бота. Пиши только факты, без воды."},
            {"role": "user", "content": text_dump}
        ]
        raw_reply, finish_reason, is_rate_limit = _chat_completion_with_retry(messages, 120)
        if is_rate_limit:
            return
        summary = clean_text(raw_reply)
        if summary:
            user_memory[user_id]["messages"] = [{"role": "system", "content": f"Факты о собеседнике: {summary}"}] + history[-4:]
            save_memory()
    except Exception as e:
        print(f"Ошибка суммаризации истории: {e}")


# ----------------- ПОВЕДЕНЧЕСКАЯ ЛОГИКА -----------------

def is_night_time() -> bool:
    hour = datetime.now().hour
    return hour >= 0 and hour < 7


def get_current_reply_chance() -> float:
    return RANDOM_REPLY_CHANCE * NIGHT_REPLY_MULTIPLIER if is_night_time() else RANDOM_REPLY_CHANCE


async def mood_change_loop():
    global current_mood
    while True:
        await asyncio.sleep(random.randint(7200, 14400))
        current_mood = random.choice(MOOD_LIST)
        print(f"[НАСТРОЕНИЕ] Новое настроение: {current_mood}")


async def memory_autosave_loop():
    while True:
        await asyncio.sleep(300)
        save_memory()


async def maybe_react(event):
    try:
        if random.random() < REACTION_CHANCE:
            emoji = random.choice(REACTION_EMOJIS)
            await event.client(SendReactionRequest(
                peer=event.chat_id,
                msg_id=event.id,
                big=False,
                add_to_recent=True,
                reaction=[ReactionEmoji(emoticon=emoji)]
            ))
    except Exception as e:
        print(f"[РЕАКЦИЯ] Не удалось поставить реакцию: {e}")


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
    STATS["replies_sent"] += 1


async def autonomous_chat_initiator():
    """Редкая активация бота (раз в 3-6 часов)."""
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
                STATS["replies_sent"] += 1
                log_to_file(0, "autonomous", "Bot_Initiative", ai_reply)
        except Exception as e:
            print(f"Ошибка фонового цикла: {e}")


# ----------------- АДМИН-КОМАНДЫ (только для OWNER_ID) -----------------

ADMIN_HELP = (
    "Доступные команды:\n"
    "!status — статус бота\n"
    "!sleep — принудительный сон на 3 часа\n"
    "!wake — выйти из режима сна\n"
    "!mood <текст> — задать настроение вручную\n"
    "!clear — очистить всю память\n"
    "!clearuser <id> — очистить память конкретного юзера\n"
    "!stats — статистика\n"
    "!say <текст> — отправить сообщение от лица бота напрямую\n"
    "!help — это сообщение"
)


async def handle_admin_command(event, text: str):
    global is_sleeping, current_mood

    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "!status":
        state = "СПИТ" if is_sleeping else ("ОФФЛАЙН" if is_offline else "АКТИВЕН")
        await event.reply(f"Состояние: {state}\nНастроение: {current_mood}\nАктивных диалогов: {len(user_memory)}")
    elif cmd == "!sleep":
        asyncio.create_task(trigger_sleep_mode())
        await event.reply("Ушёл спать на 3 часа.")
    elif cmd == "!wake":
        is_sleeping = False
        await event.reply("Проснулся.")
    elif cmd == "!mood":
        if arg:
            current_mood = arg
            await event.reply(f"Настроение установлено: {current_mood}")
        else:
            await event.reply(f"Текущее настроение: {current_mood}")
    elif cmd == "!clear":
        user_memory.clear()
        save_memory()
        await event.reply("Вся память очищена.")
    elif cmd == "!clearuser":
        try:
            uid = int(arg)
            user_memory.pop(uid, None)
            save_memory()
            await event.reply(f"Память пользователя {uid} очищена.")
        except ValueError:
            await event.reply("Нужно указать числовой ID: !clearuser 123456")
    elif cmd == "!stats":
        uptime = _format_uptime(time.time() - STATS["start_time"])
        await event.reply(
            f"Аптайм: {uptime}\n"
            f"Сообщений увидено: {STATS['messages_seen']}\n"
            f"Ответов отправлено: {STATS['replies_sent']}\n"
            f"Картинок сгенерировано: {STATS['images_generated']}\n"
            f"Попаданий в rate-limit: {STATS['rate_limit_hits']}"
        )
    elif cmd == "!say":
        if arg:
            await event.client.send_message(event.chat_id, arg)
        else:
            await event.reply("Нужно указать текст: !say привет всем")
    elif cmd == "!help":
        await event.reply(ADMIN_HELP)
    else:
        return False
    return True


# ----------------- TELEGRAM ХЕНДЛЕРЫ -----------------

def register_telegram_handlers():
    if not client or not CHAT_ID:
        return

    @client.on(events.NewMessage(chats=CHAT_ID))
    async def handle_message(event):
        global last_message_time, user_last_message_time

        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return

        user_id = sender.id
        username = getattr(sender, "username", "") or ""
        text = event.text or ""

        # 0. АДМИН-КОМАНДЫ владельца бота (работают даже во сне/оффлайне не будем — только онлайн)
        if OWNER_ID and user_id == OWNER_ID and text.startswith("!"):
            handled = await handle_admin_command(event, text)
            if handled:
                return

        if event.out or is_sleeping or is_offline:
            return

        has_photo = bool(event.photo)

        # 1. ЗАЩИТА ОТ АБУЗА: Полный игнор любых провокаций Iris и системных команд
        if IRIS_DANGER_PATTERN.search(text):
            print(f"[ОБХОД АБУЗА] Игнорируем опасную команду от user_{user_id}: {text}")
            return

        if not text and not has_photo:
            return

        STATS["messages_seen"] += 1
        log_to_file(user_id, username, "User", text or "[PHOTO]")
        me = await client.get_me()

        # Небольшой шанс просто поставить реакцию на сообщение, не отвечая текстом
        asyncio.create_task(maybe_react(event))

        is_reply_to_me = False
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.sender_id == me.id:
                is_reply_to_me = True

        is_mentioned = bool(me.username and f"@{me.username}" in text)

        # 2. ОГРАНИЧЕНИЕ ВЛЕЗАНИЯ: Личное обращение — 100%, случайное влезание — зависит от времени суток
        should_respond = is_reply_to_me or is_mentioned or (random.random() < get_current_reply_chance())

        if not should_respond:
            return

        current_time = time.time()

        # 3. КУЛДАУН: Игнорируем слишком частые запросы от одного юзера без флуда в ответ
        last_user_time = user_last_message_time.get(user_id, 0)
        if current_time - last_user_time < COOLDOWN_SECONDS and not (is_reply_to_me or is_mentioned):
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
                    STATS["images_generated"] += 1
                    log_to_file(user_id, username, "Bot", "[GENERATED_IMAGE]")
                    return

            # Обработка входящих фото
            if has_photo:
                async with event.client.action(event.chat_id, "typing"):
                    photo_bytes = await event.download_media(file=bytes)
                    ai_reply, is_rate_limit = analyze_image_with_ai(photo_bytes, caption_text=text)
                if is_rate_limit:
                    asyncio.create_task(trigger_sleep_mode())
                    return
                if ai_reply:
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    await event.reply(ai_reply)
                    STATS["replies_sent"] += 1
                    log_to_file(user_id, username, "Bot", ai_reply)
                    return

            # Текстовый ответ (с накоплением очереди сообщений пользователя)
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
                summarize_old_history(user_id)
                await send_split_messages(event, ai_reply)


# ----------------- СТАРТ И ИНИЦИАЛИЗАЦИЯ -----------------

def start_bot_tasks():
    bot_loop.create_task(autonomous_chat_initiator())
    bot_loop.create_task(internet_monitor_loop())
    bot_loop.create_task(mood_change_loop())
    bot_loop.create_task(memory_autosave_loop())


def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


async def main():
    global client, groq_client

    load_memory()
    atexit.register(save_memory)

    if GROQ_API_KEYS:
        init_groq_client()
        print(f"[GROQ] Загружено ключей: {len(GROQ_API_KEYS)}")

    if API_ID and API_HASH and SESSION_STRING:
        print("[ИНФО] Подключение юзербота через StringSession...")
        client = Telegram
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
        save_memory()
