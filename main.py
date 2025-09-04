# --- ИМПОРТЫ (коротко и без дублей) ---
import os, sys, time, threading, signal, random, re, json, hashlib, string
from http.server import BaseHTTPRequestHandler, HTTPServer
import openai
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import firebase_admin
from firebase_admin import credentials, firestore
import pytz
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import subprocess, tempfile
# ==== детектор иврита/русского ====
HEB_RE = re.compile(r'[\u0590-\u05FF]')
CYR_RE = re.compile(r'[А-Яа-яЁё]')

def contains_hebrew(s: str) -> bool:
    return bool(s and HEB_RE.search(s))

def contains_cyrillic(s: str) -> bool:
    return bool(s and CYR_RE.search(s))

# ===== БАЗОВЫЕ НАСТРОЙКИ =====
load_dotenv()

ALLOWED_ADMINS = {1037123191}  # сюда свой ID и ID подруг/дочери, если надо
tz = pytz.timezone('Asia/Jerusalem')

# OpenAI
from openai import (
    OpenAI,
    APIConnectionError,
    RateLimitError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
)

openai.api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
client = OpenAI(api_key=openai.api_key, timeout=30)

# Telegram bot
TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

def _mask_token(t: str) -> str:
    if not t: return "<empty>"
    head = t.split(":")[0]; tail = t[-4:] if len(t) >= 4 else t
    return f"{head}:***...***{tail}"

print(f"[BOOT] TOKEN: {_mask_token(TOKEN)}")
if not TOKEN or ":" not in TOKEN:
    print(f"[BOOT] TELEGRAM_BOT_TOKEN invalid. Read='{_mask_token(TOKEN)}' len={len(TOKEN)}")
    raise RuntimeError("TELEGRAM_BOT_TOKEN отсутствует или неверен. Проверь в Render → Settings → Environment.")
else:
    print(f"[BOOT] TELEGRAM_BOT_TOKEN ok: {_mask_token(TOKEN)}")

# ===== Health-check HTTP-сервер (для Render) =====
class HealthHandler(BaseHTTPRequestHandler):
    def _ok_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
    
    def do_GET(self):
        self._ok_headers()
        try:
            self.wfile.write(b"ok")
        except BrokenPipeError:
            pass
    
    def do_HEAD(self):
        self._ok_headers()

def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# ===== Ликвидация возможного webhook + анти-дубликаты =====
def clear_webhook_and_wait():
    try:
        import requests
        url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"
        response = requests.post(url)
        print(f"Webhook cleared: {response.json()}")
        print("⏳ Ждем завершения других экземпляров...")
        time.sleep(20)
    except Exception as e:
        print(f"Ошибка при очистке webhook: {e}")

clear_webhook_and_wait()

def create_bot_with_retry():
    max_retries = 3
    for attempt in range(max_retries):
        try:
            bot = telebot.TeleBot(TOKEN)
            bot.get_me()
            print(f"✅ Бот успешно инициализирован (попытка {attempt + 1})")
            return bot
        except telebot.apihelper.ApiTelegramException as e:
            if "409" in str(e):
                print(f"❌ Конфликт экземпляров (попытка {attempt + 1}). Жду...")
                time.sleep(15)
            else:
                raise e
        except Exception as e:
            print(f"Ошибка инициализации бота: {e}")
            if attempt == max_retries - 1:
                raise e
            time.sleep(5)
    raise Exception("Не удалось создать бота после всех попыток")

# === Создаём бота и объявляем версию ===
bot = create_bot_with_retry()
VERSION = "botargem-7"

# какой движок перевода использовали в последний раз для этого чата
user_engine = {}  # chat_id -> "google" | "mymemory"

def _is_admin(user_id: int) -> bool:
    return int(user_id) in ALLOWED_ADMINS

# ===== Firebase (ИДЕМПОТЕНТНО!) =====
def _find_firebase_key():
    candidates = []
    if os.getenv("FIREBASE_CREDENTIALS_PATH"):
        candidates.append(os.getenv("FIREBASE_CREDENTIALS_PATH"))
    
    repo_file = os.path.join(os.path.dirname(__file__), "trivia-game-79e1b-firebase-adminsdk-fbsvc-20be34c499.json")
    candidates.append(repo_file)
    candidates.append(os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "firebase-key.json")
    candidates.append("/etc/secrets/firebase-key.json")
    
    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError("Не найден ключ Firebase")

firebase_key_path = _find_firebase_key()
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    app = firebase_admin.initialize_app(cred)
else:
    app = firebase_admin.get_app()

db = firestore.client(app=app)
print(f"🔥 Firebase подключен: app={app.name}")

# ===== USERS: автокарточка и подписки по умолчанию =====
def _ensure_user(user):
    """Создает/обновляет запись пользователя в Firebase"""
    uid = str(user.id)
    db.collection("users").document(uid).set({
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "sub_pod": True,  # Фраза дня: по умолчанию включена
        "sub_fact": True,  # Факт дня: по умолчанию включён
        "last_seen": datetime.utcnow().isoformat(),
    }, merge=True)
def _send_explanation_guard(chat_id: int, body: str, offline: bool = False):
    """
    Если ответ получился целиком на иврите (без кириллицы) — не шлём «простыню»,
    а даём понятное сообщение. Иначе отправляем объяснение.
    """
    if contains_hebrew(body) and not contains_cyrillic(body):
        bot.send_message(
            chat_id,
            "🛠 Произошёл сбой: объяснение вышло на иврите.\n"
            "Попробуйте ещё раз нажать «🧠 Объяснить»."
        )
        return
    prefix = "🧠 Объяснение (офлайн):\n" if offline else "🧠 Объяснение:\n"
    bot.send_message(chat_id, prefix + body)
# ===== ДОСТУП: только ID из allowed_users =====
ALLOWED_USERS = set()

def load_allowed_users():
    try:
        users_ref = db.collection("allowed_users").stream()
        for doc in users_ref:
            ALLOWED_USERS.add(int(doc.id))
        print(f"✅ Загружено {len(ALLOWED_USERS)} пользователей из Firebase")
    except Exception as e:
        print(f"Ошибка при загрузке пользователей: {e}")

load_allowed_users()

def check_access(user_id: int) -> bool:
    # Разрешаем всем + логируем, чтобы точно видеть в Render Logs
    try:
        print(f"[access] ALLOW user={user_id}")
    except Exception:
        pass
    return True

# ===== Админ: владелец (только ты) =====
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
if not OWNER_ID and ALLOWED_USERS:
    OWNER_ID = sorted(ALLOWED_USERS)[0]
print(f"👑 OWNER_ID = {OWNER_ID or 'не задан'}")

def is_owner(user_id: int) -> bool:
    return OWNER_ID and (user_id == OWNER_ID)

# ===== ЛИМИТЫ / ПРЕМИУМ / ДОНАТЫ =====
FREE_LIMIT_TEXT = 3
FREE_LIMIT_AUDIO = 3
TEXT_MAX_LEN_PER_MSG = 500
TEXT_MAX_LEN_PER_DAY = 1500
AUDIO_MAX_SEC_PER_MSG = 60
AUDIO_MAX_SEC_PER_DAY = 180

TEXT_TOO_LONG_MSG = f"⚠️ Сообщение слишком длинное. Максимум {TEXT_MAX_LEN_PER_MSG} символов за раз. Разбей на части 🙂"
AUDIO_TOO_LONG_MSG = f"⚠️ Голосовое слишком длинное. Максимум {AUDIO_MAX_SEC_PER_MSG} секунд за раз. Попробуй короче 🙂"

DONATE_LINKS = [
    ("🍰 PayBox", "https://links.payboxapp.com/FqQZPo2wfWb"),
]

# где искать картинку для Bit
BIT_QR_IMAGE = (
    os.getenv("BIT_QR_LOCAL_PATH") or 
    os.path.join(os.path.dirname(__file__), "bit_qr.jpg")
)

def _usage_doc_ref(user_id: int, date_iso: str):
    return db.collection("usage").document(f"{user_id}_{date_iso}")

def _today_iso():
    return datetime.now(tz).date().isoformat()

def get_usage(user_id: int) -> dict:
    today = _today_iso()
    try:
        ref = _usage_doc_ref(user_id, today)
        snap = ref.get()
        if snap.exists:
            d = snap.to_dict()
        else:
            d = {"text": 0, "audio": 0, "text_chars": 0, "audio_secs": 0}
            ref.set(d)
        
        d.setdefault("text", 0)
        d.setdefault("audio", 0)
        d.setdefault("text_chars", 0)
        d.setdefault("audio_secs", 0)
        return d
    except Exception:
        # локальный фолбэк
        global _LOCAL_USAGE
        try:
            _LOCAL_USAGE
        except NameError:
            _LOCAL_USAGE = {}
        
        info = _LOCAL_USAGE.setdefault(user_id, {})
        if info.get("date") != today:
            info.update({"date": today, "text": 0, "audio": 0, "text_chars": 0, "audio_secs": 0})
        return info

def save_usage(user_id: int, data: dict):
    today = _today_iso()
    try:
        _usage_doc_ref(user_id, today).set(data, merge=True)
    except Exception:
        global _LOCAL_USAGE
        _LOCAL_USAGE[user_id] = {**_LOCAL_USAGE.get(user_id, {}), **data, "date": today}

# ===== PREMIUM (ручное включение по чеку) =====
def is_premium(user_id: int) -> bool:
    try:
        doc = db.collection("premium_users").document(str(user_id)).get()
        d = doc.to_dict() or {}
        if not d.get("active"):
            return False
        
        until = d.get("until")
        if until:
            return datetime.now(tz).date().isoformat() <= until
        return True
    except Exception:
        return False

def can_use(user_id: int, kind: str) -> bool:
    # по 3 шт/день; премиум — безлимит
    if is_premium(user_id):
        return True
    
    d = get_usage(user_id)
    if kind == "text":
        if d["text"] < FREE_LIMIT_TEXT:
            d["text"] += 1
            save_usage(user_id, d)
            return True
        return False
    
    if kind == "audio":
        if d["audio"] < FREE_LIMIT_AUDIO:
            d["audio"] += 1
            save_usage(user_id, d)
            return True
        return False
    
    return False

def can_use_text_volume(user_id: int, msg_len: int) -> tuple[bool, str]:
    if is_premium(user_id):
        if msg_len > 2000:
            return False, "⚠️ Очень длинное сообщение. Разбей, пожалуйста."
        return True, ""
    
    if msg_len > TEXT_MAX_LEN_PER_MSG:
        return False, TEXT_TOO_LONG_MSG
    
    d = get_usage(user_id)
    if d["text_chars"] + msg_len > TEXT_MAX_LEN_PER_DAY:
        left = max(0, TEXT_MAX_LEN_PER_DAY - d["text_chars"])
        return False, f"🚫 Лимит символов на сегодня исчерпан. Осталось: {left}/{TEXT_MAX_LEN_PER_DAY}. Завтра обнулится."
    
    d["text_chars"] += msg_len
    save_usage(user_id, d)
    return True, ""

def can_use_audio_volume(user_id: int, duration_sec: int) -> tuple[bool, str]:
    if is_premium(user_id):
        if duration_sec > 600:
            return False, "⚠️ Очень длинное аудио. Сделай короче, пожалуйста."
        return True, ""
    
    if duration_sec > AUDIO_MAX_SEC_PER_MSG:
        return False, AUDIO_TOO_LONG_MSG
    
    d = get_usage(user_id)
    if d["audio_secs"] + duration_sec > AUDIO_MAX_SEC_PER_DAY:
        left = max(0, AUDIO_MAX_SEC_PER_DAY - d["audio_secs"])
        return False, f"🚫 Лимит длительности аудио исчерпан. Осталось: {left} сек. из {AUDIO_MAX_SEC_PER_DAY}. Завтра обнулится."
    
    d["audio_secs"] += duration_sec
    save_usage(user_id, d)
    return True, ""

def limit_msg(kind):
    if kind == "text":
        return "🚫 Лимит *текстовых* переводов (3) исчерпан. 🔄 Сброс в полночь. Нужен безлимит? /premium"
    else:
        return "🚫 Лимит *аудио* переводов (3) исчерпан. 🔄 Сброс в полночь. Нужен безлимит? /premium"

# ===== Переводчики =====
from deep_translator import GoogleTranslator, MyMemoryTranslator

def translate_text(text: str) -> str:
    """Стабильный перевод: сначала deep-translator, при ошибке — MyMemory."""
    src = "iw" if HEB_RE.search(text) else "auto"
    try:
        return GoogleTranslator(source=src, target="ru").translate(text)
    except Exception as e1:
        print(f"[translate_text] deep-translator error: {e1}")
        try:
            return MyMemoryTranslator(source=src, target="ru").translate(text)
        except Exception as e2:
            print(f"[translate_text] MyMemory error: {e2}")
            return "⚠️ Ошибка перевода"

def translate_with_engine(text: str, engine: str) -> tuple[str, str]:
    """Перевод строго выбранным движком. Возвращает (перевод, использованный_движок)."""
    src = "iw" if HEB_RE.search(text) else "auto"
    try:
        if engine == "mymemory":
            return MyMemoryTranslator(source=src, target="ru").translate(text), "mymemory"
        else:
            return GoogleTranslator(source=src, target="ru").translate(text), "google"
    except Exception as e:
        other = "google" if engine == "mymemory" else "mymemory"
        try:
            if other == "mymemory":
                return MyMemoryTranslator(source=src, target="ru").translate(text), "mymemory"
            else:
                return GoogleTranslator(source=src, target="ru").translate(text), "google"
        except Exception as e2:
            print(f"[translate_with_engine] оба упали: {e} / {e2}")
            return "⚠️ Ошибка перевода", engine

# === Фильтр «осмысленного» текста ===
HEB = r"\u0590-\u05FF"
LAT = r"A-Za-z"
CYR = r"А-Яа-яЁё"
LETTER_RE = re.compile(fr"[{HEB}{LAT}{CYR}]")
PUNCT = set(string.punctuation + "…—–«»""'‚·•")

def _strip_noise(s: str) -> str:
    # убираем невидимые zero-width и пробелы по краям
    return (s or "").replace("\u200d", "").replace("\u200c", "").strip()

def _looks_like_only_punct_or_emoji(s: str) -> bool:
    # нет ни одной буквы, и все символы — не буквенно-цифровые (пунктуация/эмодзи)
    no_letters = LETTER_RE.search(s) is None
    only_non_alnum = all((not ch.isalnum()) for ch in s)
    return no_letters and only_non_alnum

def is_meaningful_text(s: str) -> bool:
    s = _strip_noise(s)
    if not s:
        return False
    if s.startswith("/"):  # команды пропускаем
        return True
    if len(s) == 1 and not s.isalnum():
        return False  # одиночная точка и т.п.
    if _looks_like_only_punct_or_emoji(s):
        return False
    # односимвольные «слова» без букв (например, "1", "#") — отклоняем
    if len(s) < 2 and LETTER_RE.search(s) is None:
        return False
    return True

# ---- офлайн-фолбэк для "Объяснить" ----
IDIOMS = {
    "יאללה": "Сленг: «давай/погнали/ну же».",
    "סבבה": "Сленг: «окей, супер, норм».",
    "באסה": "Сленг: «облом, неприятность».",
    "תכלס": "Сленг: «по сути, по факту».",
    "כפרה": "Ласковое обращение: «душа моя».",
    "אין מצב": "«Ни за что / да ну!» — удивление/отказ.",
    "די נו": "«Хватит уже / да ну».",
    "מה נסגר איתך": "«Что с тобой происходит?» — разговорное.",
}

def explain_local(he_text: str) -> str:
    tr = translate_text(he_text)
    hits = []
    low = he_text.replace("׳", "").replace("'", "").replace("", "")
    
    for k, note in IDIOMS.items():
        if k in low or k.replace("׳", "") in low:
            hits.append(f"• *{k}* — {note}")
    
    note_block = "\n".join(hits) if hits else "Сленг/идиом не найдено."
    
    return (
        f"Перевод: {tr}\n\n"
        f"Сленг/идиомы:\n{note_block}\n\n"
        f"Грамматика: разговорная речь; для точного морфоразбора нужен онлайн-режим."
    )

# ===== Аудио обработка =====
def _tg_download_to_tmp(message):
    """Скачивает voice/audio/document → возвращает путь к локальному файлу."""
    if message.voice:
        file_id = message.voice.file_id
        ext = ".ogg"
    elif message.audio:
        file_id = message.audio.file_id
        ext = os.path.splitext(message.audio.file_name or "")[1] or ".m4a"
    elif message.document:
        file_id = message.document.file_id
        ext = os.path.splitext(message.document.file_name or "")[1] or ".bin"
    else:
        raise RuntimeError("Неизвестный тип аудио")
    
    f = bot.get_file(file_id)
    raw = bot.download_file(f.file_path)
    fd, path = tempfile.mkstemp(prefix="audio_", suffix=ext)
    os.close(fd)
    
    with open(path, "wb") as out:
        out.write(raw)
    return path

def _ensure_ogg(input_path):
    """Если уже ogg/opus — вернём как есть. Иначе перекодируем в ogg 16kHz mono."""
    low = input_path.lower()
    if low.endswith(".ogg") or low.endswith(".oga"):
        return input_path
    
    out_path = os.path.splitext(input_path)[0] + ".ogg"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", "-c:a", "libopus", out_path],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return out_path

def process_audio(message):
    chat_id = message.chat.id
    try:
        # 1) скачали voice/audio/document
        local_file = _tg_download_to_tmp(message)
        # 2) привели к ogg 16kHz mono (если нужно)
        file_for_stt = _ensure_ogg(local_file)

        # 3) распознали речь → текст
        with open(file_for_stt, "rb") as f:
            tr = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f
            )
        text = getattr(tr, "text", "").strip()

        if not text:
            bot.send_message(chat_id, "⚠️ Не удалось распознать речь.")
            return

        # 🔹 НОВОЕ: фильтр — если в расшифровке нет иврита, вежливо выходим
        if not contains_hebrew(text):
            bot.send_message(
                chat_id,
                "🌸 Я перевожу *только с иврита на русский*.\n"
                "Пришлите, пожалуйста, аудио на иврите.",
                parse_mode="Markdown"
            )
            return

        # 4) перевод распознанного текста
        translated = translate_text(text)

        # 5) сохранить текст для кнопок «🧠 Объяснить» и «🔁 Новый перевод»
        user_translations[chat_id] = text
        user_engine[chat_id] = "google"

        # 6) показать всё одним сообщением + кнопки
        msg = (
            f"📝 Расшифровка:\n{text}\n\n"
            f"📘 Перевод:\n*{translated}*"
        )
        bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=get_keyboard())

        # 7) история
        try:
            add_history(message.from_user.id, "audio", text, translated)
        except Exception as e:
            print("[history audio] err:", e)

    except Exception as e:
        print("Ошибка аудио:", e)
        bot.send_message(chat_id, "⚠️ Ошибка при расшифровке аудио.")


# ===== История переводов =====
def _history_ref(user_id: int):
    return db.collection("users").document(str(user_id)).collection("history")

def add_history(user_id: int, kind: str, source: str, result: str):
    try:
        _history_ref(user_id).add({
            "ts": datetime.utcnow().isoformat(),
            "kind": kind,  # "text" | "audio"
            "source": (source or "")[:4000],
            "result": (result or "")[:4000],
        })
    except Exception as e:
        print("[history] err:", e)

# ===== UI-кнопки перевода =====
def get_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🧠 Объяснить", callback_data="explain"),
        InlineKeyboardButton("🔁 Новый перевод", callback_data="new")
    )
    return markup

def get_yes_no_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Перевести", callback_data="translate_forwarded"),
        InlineKeyboardButton("❌ Нет", callback_data="cancel")
    )
    return markup

# === Состояние отправки чека ===
receipt_state = {}  # chat_id -> {"provider": "paybox", "ts": datetime.utcnow().isoformat()}

# PayBox: распознаём ссылку или «признаки» в тексте
PAYBOX_URL_RE = re.compile(r"https?://\S*payboxapp\.com/\S+", re.I)
AMOUNT_RE = re.compile(r"(\d+[.,]?\d*)\s*(₪|шек|nis|ש״ח)", re.I)  # число + валюта/₪

# === Состояние пользователей ===
user_translations = {}
user_data = {}

# ===== ФРАЗА ДНЯ =====
FALLBACK_PHRASES = [
    {"he": "סבבה", "ru": "окей; норм", "note": "разговорное «ок»"},
    {"he": "אין בעיה", "ru": "без проблем", "note": ""},
    {"he": "יאללה, נתקדם", "ru": "ну поехали, двигаемся", "note": ""},
    {"he": "בא לי קפה", "ru": "мне хочется кофе", "note": "בא לי — «мне хочется»"},
    {"he": "כמה זה יוצא?", "ru": "сколько выходит?", "note": "про цену/итог"},
    {"he": "סגרתי פינה", "ru": "закрыла вопрос; разобралась", "note": "сленг"},
    {"he": "יאללה, זזתי", "ru": "ладно, я пошла", "note": "букв. «двинулась»"},
    {"he": "שניה, אני בודקת", "ru": "секунду, я проверю", "note": ""},
]

def load_phrase_db():
    path = os.getenv("PHRASES_FILE", "phrases.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list) and all(("he" in x and "ru" in x) for x in data)
        print(f"[phrases] loaded {len(data)} from {path}")
        return data
    except Exception as e:
        print(f"[phrases] using FALLBACK (reason: {e})")
        return FALLBACK_PHRASES

phrase_db = load_phrase_db()

def _today_idx():
    today = datetime.now(tz).date().isoformat()
    h = int(hashlib.sha1(today.encode("utf-8")).hexdigest(), 16)
    return h % len(phrase_db)

def phrase_of_today():
    return phrase_db[_today_idx()]

def build_pod_message(item):
    return (
        "☀️ בוקר טוב!\nВот тебе фраза дня:\n\n"
        f"🗣 *{item['he']}*\n"
        f"📘 Перевод: _{item['ru']}_\n"
        f"💬 Пояснение: {item.get('note', '—')}"
    )

def _get_last_pod_date(user_id):
    doc = db.collection("users").document(str(user_id)).get()
    d = doc.to_dict() or {}
    return d.get("last_pod")

def _set_last_pod_date(user_id, date_iso):
    db.collection("users").document(str(user_id)).set({"last_pod": date_iso}, merge=True)

def send_phrase_of_the_day_now():
    item = phrase_of_today()
    today = datetime.now(tz).date().isoformat()
    msg = build_pod_message(item)
    
    # Все пользователи, у кого включена подписка sub_pod
    try:
        recipients = [int(doc.id) for doc in db.collection("users").where("sub_pod", "==", True).stream()]
    except Exception as e:
        print(f"[pod] recipients err: {e}")
        recipients = []
    
    for user_id in recipients:
        if _get_last_pod_date(user_id) == today:
            continue
        try:
            bot.send_message(user_id, msg, parse_mode="Markdown")
            _set_last_pod_date(user_id, today)
        except Exception as e:
            print(f"[pod] send failed for {user_id}: {e}")

def _schedule_next_8am():
    now = datetime.now(tz)
    next8 = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= next8:
        next8 += timedelta(days=1)
    delay = (next8 - now).total_seconds()
    
    def _run():
        try:
            send_phrase_of_the_day_now()
        finally:
            _schedule_next_8am()
    
    threading.Timer(delay, _run).start()

_schedule_next_8am()

# ===== ФАКТ ДНЯ (20:00) =====
FALLBACK_FACTS = [
    {"he": "המילה שלום משמשת כברכה וגם כפרידה.", "ru": "«Шалом» — и приветствие, и прощание.", "note": "Также означает «мир»."},
]

def _load_facts_file():
    path = os.getenv("FACTS_FILE", "facts.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            arr = json.load(f)
        if isinstance(arr, list) and arr:
            return arr
    except Exception as e:
        print(f"[facts] fallback: {e}")
    return FALLBACK_FACTS

FACTS_DB = _load_facts_file()

def _random_fact():
    try:
        docs = list(db.collection("facts").stream())
        if docs:
            d = random.choice(docs).to_dict()
            return {"he": d.get("he", ""), "ru": d.get("ru", ""), "note": d.get("note", "")}
    except Exception as e:
        print(f"[facts] FS err: {e}")
    return random.choice(FACTS_DB)

def build_fact_message(item):
    msg = f"📜 *Факт дня*\n\n🗣 {item.get('he', '')}\n📘 Перевод: {item.get('ru', '')}"
    if item.get("note"):
        msg += f"\n💡 {item['note']}"
    return msg

def _get_last_fact_date(user_id):
    doc = db.collection("users").document(str(user_id)).get()
    d = doc.to_dict() or {}
    return d.get("last_fact")

def _set_last_fact_date(user_id, date_iso):
    db.collection("users").document(str(user_id)).set({"last_fact": date_iso}, merge=True)

def send_fact_of_the_day_now():
    item = _random_fact()
    today = datetime.now(tz).date().isoformat()
    msg = build_fact_message(item)
    
    # Все пользователи, у кого включена подписка sub_fact
    try:
        recipients = [int(doc.id) for doc in db.collection("users").where("sub_fact", "==", True).stream()]
    except Exception as e:
        print(f"[fact] recipients err: {e}")
        recipients = []
    
    for user_id in recipients:
        if _get_last_fact_date(user_id) == today:
            continue
        try:
            bot.send_message(user_id, msg, parse_mode="Markdown")
            _set_last_fact_date(user_id, today)
        except Exception as e:
            print(f"[fact] send failed for {user_id}: {e}")

def _schedule_next_20():
    now = datetime.now(tz)
    next20 = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if now >= next20:
        next20 += timedelta(days=1)
    delay = (next20 - now).total_seconds()
    
    def _run():
        try:
            send_fact_of_the_day_now()
        finally:
            _schedule_next_20()
    
    threading.Timer(delay, _run).start()

_schedule_next_20()

# ===== ВИКТОРИНА =====
QUIZ_COLL = "quiz"
QUIZ_DOC = "current"
QUIZ_STATS_DOC = "stats"

def _u(user_id):
    return db.collection("users").document(str(user_id))

def _quiz_state_ref(user_id):
    return _u(user_id).collection(QUIZ_COLL).document(QUIZ_DOC)

def _quiz_stats_ref(user_id):
    return _u(user_id).collection(QUIZ_COLL).document(QUIZ_STATS_DOC)

def _mk_options(correct_ru, all_ru, k=3):
    pool = [x for x in all_ru if x and x != correct_ru]
    random.shuffle(pool)
    distractors, seen = [], set()
    
    for v in pool:
        if v not in seen:
            seen.add(v)
            distractors.append(v)
        if len(distractors) >= k:
            break
    
    opts = [correct_ru] + distractors
    random.shuffle(opts)
    answer_idx = opts.index(correct_ru)
    return opts, answer_idx

def _choose_question():
    if not isinstance(phrase_db, list) or len(phrase_db) < 4:
        raise RuntimeError("Для викторины нужно минимум 4 фразы в phrase_db.")
    
    item = random.choice(phrase_db)
    he, ru, note = item.get("he"), item.get("ru"), (item.get("note") or "")
    
    if not he or not ru:
        return _choose_question()
    
    all_ru = [x.get("ru") for x in phrase_db if x.get("ru")]
    options, answer_idx = _mk_options(ru, all_ru, k=3)
    
    return {
        "he": he,
        "ru": ru,
        "note": note,
        "options": options,
        "answer": answer_idx,
        "ts": datetime.utcnow().isoformat(),
        "done": False
    }

def _render_quiz_message(state):
    he = state["he"]
    opts = state["options"]
    lines = ["🧠 *Викторина*", "Выбери перевод фразы на иврите:", f"🗣 {he}", "", "Варианты:"]
    
    for i, opt in enumerate(opts, start=1):
        lines.append(f"{i}. {opt}")
    
    return "\n".join(lines)

def _quiz_keyboard(state):
    kb = InlineKeyboardMarkup()
    for i in range(len(state["options"])):
        kb.add(InlineKeyboardButton(f"Выбрать {i+1}", callback_data=f"qz:pick:{i}"))
    kb.add(InlineKeyboardButton("Стоп", callback_data="qz:stop"))
    return kb

def _again_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("Ещё", callback_data="qz:again"), InlineKeyboardButton("Стоп", callback_data="qz:stop"))
    return kb

def _inc_stats(user_id, correct: bool):
    ref = _quiz_stats_ref(user_id)
    snap = ref.get()
    d = snap.to_dict() if snap.exists else {"total": 0, "correct": 0}
    d["total"] = int(d.get("total", 0)) + 1
    if correct:
        d["correct"] = int(d.get("correct", 0)) + 1
    ref.set(d, merge=True)
    return d

def _reset_current(user_id):
    _quiz_state_ref(user_id).delete()

# ===== Функция для работы с OpenAI =====
def ask_gpt(messages, model="gpt-4o", max_retries=3):
    """Запрос к OpenAI с ретраями и экспоненциальной паузой."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.4,
                timeout=30,
                max_tokens=300
            )
            return resp.choices[0].message.content.strip()
        except (APIConnectionError, RateLimitError, APIStatusError) as e:
            print(f"[ask_gpt] API error (попытка {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                return None
        except (AuthenticationError, BadRequestError) as e:
            print(f"[ask_gpt] Auth/BadRequest error: {e}")
            raise
        except Exception as e:
            print(f"[ask_gpt] Unexpected error (попытка {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                return None
        
        if attempt < max_retries:
            sleep_time = delay + random.uniform(0, 0.5)
            print(f"[ask_gpt] Ждём {sleep_time:.1f} секунд перед повтором...")
            time.sleep(sleep_time)
            delay *= 2
    
    return None

# ===== Функции для работы с чеками =====
def _forward_receipt_to_owner(chat_id: int, from_user, text_summary: str, photo_message=None):
    uid = from_user.id
    uname = from_user.username or "—"
    header = f"📩 Чек на премиум (PayBox)\nID: {uid}\nUsername: @{uname}\n{('-'*20)}\n{text_summary}"
    
    if OWNER_ID:
        try:
            if photo_message:
                # переслать фото
                bot.forward_message(OWNER_ID, photo_message.chat.id, photo_message.message_id)
            bot.send_message(OWNER_ID, header)
        except Exception as e:
            print(f"[receipt->owner {OWNER_ID}] err:", e)

def _accept_receipt_message(message) -> bool:
    """Пытается принять сообщение как чек PayBox. Возвращает True, если принято."""
    state = receipt_state.get(message.chat.id)
    if not state or state.get("provider") != "paybox":
        return False  # сейчас мы не ждём чек от этого чата
    
    # TEXT-вариант: ссылка/текст с суммой
    if message.content_type == 'text':
        txt = (message.text or "").strip()
        has_link = bool(PAYBOX_URL_RE.search(txt))
        amount = AMOUNT_RE.search(txt)
        
        if has_link or amount:
            parts = []
            if has_link:
                parts.append(f"Ссылка: {PAYBOX_URL_RE.search(txt).group(0)}")
            if amount:
                parts.append(f"Сумма: {amount.group(1)} {amount.group(2)}")
            summary = "\n".join(parts) or txt[:200]
            
            _forward_receipt_to_owner(message.chat.id, message.from_user, summary)
            bot.send_message(message.chat.id, "✅ Спасибо! Чек отправлен администратору. Премиум активируем в течение суток.")
            receipt_state.pop(message.chat.id, None)
            return True
        else:
            bot.send_message(message.chat.id, "❌ Не похоже на чек PayBox. Пришлите ссылку PayBox или скриншот с подписью (сумма + дата).")
            return True  # обработано (но не принято)
    
    # PHOTO-вариант: скрин с подписью
    if message.content_type == 'photo':
        caption = (message.caption or "").strip()
        amount = AMOUNT_RE.search(caption)
        
        if not amount:
            bot.send_message(message.chat.id, "ℹ️ Добавьте подпись к скрину: *сумма* и *дата/время*. Пример: 15₪, 02.09 10:35", parse_mode="Markdown")
            return True  # обработано
        
        summary = f"Скриншот PayBox\nСумма: {amount.group(1)} {amount.group(2)}\nПодпись: {caption[:120]}"
        _forward_receipt_to_owner(message.chat.id, message.from_user, summary, photo_message=message)
        bot.send_message(message.chat.id, "✅ Спасибо! Чек отправлен администратору. Премиум активируем в течение суток.")
        receipt_state.pop(message.chat.id, None)
        return True
    
    # другие типы — отклоняем
    bot.send_message(message.chat.id, "Я могу принять *ссылку PayBox* или *скриншот* (с подписью: сумма + дата).", parse_mode="Markdown")
    return True

# ===== Подписки: UI =====
def _subs_kb(sub_pod: bool, sub_fact: bool):
    kb = InlineKeyboardMarkup()
    if sub_pod:
        kb.add(InlineKeyboardButton("☀️ Фраза дня: выключить", callback_data="subs:pod:off"))
    else:
        kb.add(InlineKeyboardButton("☀️ Фраза дня: включить", callback_data="subs:pod:on"))
    
    if sub_fact:
        kb.add(InlineKeyboardButton("📜 Факт дня: выключить", callback_data="subs:fact:off"))
    else:
        kb.add(InlineKeyboardButton("📜 Факт дня: включить", callback_data="subs:fact:on"))
    
    return kb

# ===== Форматирование прогресс-бара =====
def _fmt_bar(used: int, total: int, size: int = 10) -> str:
    if total <= 0:
        return "—"
    filled = int(round(size * min(used, total) / total))
    return "█" * filled + "░" * (size - filled)

# ===== КОМАНДЫ БОТА =====

@bot.message_handler(commands=['version'])
def cmd_version(m):
    bot.send_message(m.chat.id, f"Версия кода: {VERSION}")

@bot.message_handler(commands=['access'])
def cmd_access(m):
    ok = check_access(m.from_user.id)
    bot.send_message(m.chat.id, f"ACCESS={ok} user_id={m.from_user.id}\nVERSION={VERSION}")

@bot.message_handler(commands=['id'])
def send_user_id(message):
    bot.send_message(message.chat.id, f"👤 Твой Telegram ID: {message.from_user.id}", parse_mode='Markdown')

@bot.message_handler(commands=['start'])
def cmd_start(m):
    _ensure_user(m.from_user)
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("/quiz"), KeyboardButton("/quizstats"))
    kb.row(KeyboardButton("/id"), KeyboardButton("/profile"))
    bot.send_message(
        m.chat.id,
        "Привет! Я перевожу и объясняю иврит.\n"
        "• Пришли фразу или перешли сообщение — дам перевод\n"
        "• Под переводом будут кнопки «🧠 Объяснить» и «🔁 Ещё перевод»\n"
        "• Голосовые тоже можно — пришли аудио\n"
        "• Мини-игра: /quiz\n"
        "• Лимиты смотри: /profile\n"
        "© 2025 Botargem. Все права защищены",
        reply_markup=kb
    )

HELP_TEXT = (
    "👋 Привет! Вот что умеет Botargem:\n\n"
    "📝 *Перевод*\n"
    "• Пришли сообщение на иврите — получишь перевод на русский\n"
    "• Можно пересылать чужие сообщения или аудио\n\n"
    "🧠 *Объяснения*\n"
    "• Под переводом кнопка «Объяснить» — грамматика, сленг, примеры\n\n"
    "🎮 *Игры*\n"
    "• Мини-викторина: /quiz\n"
    "• Твой счёт: /quizstats\n\n"
    "☀️ Каждый день утром — «Фраза дня»\n"
    "📜 Каждый вечер — «Факт дня»\n\n"
    "⚖️ *Лимиты (без премиум)*\n"
    "• 3 текста в день\n"
    "• 3 аудио в день\n"
    "• 1500 символов текста\n"
    "• 180 секунд аудио\n"
    "Посмотреть остаток: /profile\n\n"
    "💎 *Premium*\n"
    "• С премиум-статусом лимиты не действуют\n\n"
    "💝 *Поддержка проекта*\n"
    "• Донаты: /donate (Bit QR или PayBox)\n"
)

@bot.message_handler(commands=['help'])
def cmd_help(m):
    bot.send_message(m.chat.id, HELP_TEXT, parse_mode="Markdown")

@bot.message_handler(commands=['rules', 'правила'])
def send_rules(m):
    rules_text = (
        "📜 Правила пользования ботом Botargem\n\n"
        "1. Доступ\n"
        "Все могут пользоваться ботом бесплатно в пределах дневных лимитов.\n"
        "Премиум-доступ активирует админ.\n\n"
        "2. Что умеет бот\n"
        "📝 Перевод текста и аудио\n"
        "🧠 Объяснения грамматики и сленга\n"
        "🎮 Игры и викторины\n"
        "📚 Фраза дня\n\n"
        "3. Ограничения\n"
        "🆓 Бесплатно — с лимитами\n"
        "💎 Премиум — без ограничений\n\n"
        "4. Что запрещено\n"
        "❌ Спам\n"
        "❌ Оскорбления/незаконное\n"
        "❌ Передавать премиум другим\n\n"
        "5. Важно знать\n"
        "⚠️ Возможны ошибки в переводе\n"
        "⚠️ Ответственность на пользователе\n\n"
        "6. Поддержка\n"
        "Вопросы: t.me/BotargemBot"
    )
    bot.send_message(m.chat.id, rules_text, parse_mode="Markdown")

@bot.message_handler(commands=['copyrights'])
def send_copyrights(m):
    text = (
        "🔒 Авторские права \n"
        "© 2025 Botargem. Все права защищены.\n"
        "© 2025 ‏Botargem. כל הזכויות שמורות.\n\n"
        "RU:\n"
        "• Дизайн, тексты интерфейса, база фраз и логотип 🦉 — собственность автора.\n"
        "• Нельзя копировать или публиковать без разрешения.\n"
        "• Переводы можно использовать лично, но нельзя продавать как свой сервис.\n"
        "• Ваши сообщения/аудио остаются вашими; отправляя их, вы разрешаете обработку для перевода/объяснений.\n"
        "• Медиа в промо — с разрешением или по открытой лицензии.\n"
        "HE:\n"
        "• העיצוב, הטקסטים, מאגר הביטויים והלוגו 🦉 הם קניין של היוצר.\n"
        "• אין להעתיק או לפרסם ללא אישור.\n"
        "• מותר שימוש אישי בתרגומים, אסור למכור כשירות משלכם.\n"
        "• התוכן שאתם שולחים (טקסט/אודיו) נשאר שלכם; בשליחתו אתם מאשרים שימוש לצורך תרגום/הסבר.\n"
        "• מדיה בפרומו — ברישיון מתאים או חופשי.\n"
    )
    bot.send_message(m.chat.id, text)

@bot.message_handler(commands=['profile'])
def cmd_profile(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    
    data = get_usage(m.from_user.id)
    t_used = int(data.get("text", 0))
    a_used = int(data.get("audio", 0))
    tc_used = int(data.get("text_chars", 0))
    as_used = int(data.get("audio_secs", 0))
    
    t_total, a_total = FREE_LIMIT_TEXT, FREE_LIMIT_AUDIO
    tc_total, as_total = TEXT_MAX_LEN_PER_DAY, AUDIO_MAX_SEC_PER_DAY
    
    bar_t = _fmt_bar(t_used, t_total)
    bar_a = _fmt_bar(a_used, a_total)
    bar_tc = _fmt_bar(tc_used, tc_total)
    bar_as = _fmt_bar(as_used, as_total)
    
    now = datetime.now(tz)
    midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
    left = max(0, int((midnight - now).total_seconds()))
    hh, mm = left//3600, (left%3600)//60
    
    msg = (
        "👤 *Твой профиль / лимиты на сегодня*\n\n"
        f"📝 Тексты: {t_used}/{t_total} {bar_t}\n"
        f"🔊 Аудио: {a_used}/{a_total} {bar_a}\n"
        f"🔡 Символы: {tc_used}/{tc_total} {bar_tc}\n"
        f"⏱ Секунды: {as_used}/{as_total} {bar_as}\n\n"
        f"🔄 Сброс ~через {hh}ч {mm}м (Asia/Jerusalem)"
    )
    bot.send_message(m.chat.id, msg, parse_mode="Markdown")

@bot.message_handler(commands=['menu'])
def cmd_menu(m):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📘 Переводы", callback_data="menu:tr"),
        InlineKeyboardButton("🎮 Игры", callback_data="menu:games"),
    )
    kb.row(
        InlineKeyboardButton("☀️ Фраза дня", callback_data="menu:pod"),
        InlineKeyboardButton("📜 Факт дня", callback_data="menu:fact"),
    )
    kb.row(
        InlineKeyboardButton("👤 Профиль", callback_data="menu:profile"),
        InlineKeyboardButton("💎 Premium", callback_data="menu:premium"),
    )
    bot.send_message(m.chat.id, "Главное меню — выберите раздел 👇", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("menu:"))
def cb_menu(c):
    kind = c.data.split(":",1)[1]
    if kind == "tr":
        try:
            bot.edit_message_text(
                "📘 *Переводы*\n"
                "• Пришлите *текст или аудио на иврите* — я переведу на русский.\n"
                "• Под переводом будут кнопки: «🧠 Объяснить», «🔁 Новый перевод».",
                c.message.chat.id, c.message.message_id, parse_mode="Markdown"
            )
        except Exception:
            bot.send_message(c.message.chat.id,
                "📘 *Переводы*\n"
                "• Пришлите *текст или аудио на иврите* — я переведу на русский.\n"
                "• Под переводом будут кнопки: «🧠 Объяснить», «🔁 Новый перевод».",
                parse_mode="Markdown"
            )
    elif kind == "games":
        try:
            bot.edit_message_text(
                "🎮 *Игры*\n"
                "• /quiz — мини-викторина\n"
                "• /quizstats — мой счёт",
                c.message.chat.id, c.message.message_id, parse_mode="Markdown"
            )
        except Exception:
            bot.send_message(c.message.chat.id,
                "🎮 *Игры*\n"
                "• /quiz — мини-викторина\n"
                "• /quizstats — мой счёт",
                parse_mode="Markdown"
            )
    elif kind == "pod":
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "☀️ Фраза дня приходит *в 08:00*. Управление подпиской: /subs", parse_mode="Markdown")
    elif kind == "fact":
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "📜 Факт дня приходит *в 20:00*. Управление подпиской: /subs", parse_mode="Markdown")
    elif kind == "profile":
        # Переиспользуем твою функцию профиля:
        cmd_profile(type("obj",(object,),{"chat":c.message.chat, "from_user":c.from_user}))
    elif kind == "premium":
        cmd_premium(type("obj",(object,),{"chat":c.message.chat, "from_user":c.from_user}))
    bot.answer_callback_query(c.id)


@bot.message_handler(commands=['quiz'])
def cmd_quiz(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    
    try:
        state = _choose_question()
    except Exception as e:
        return bot.send_message(m.chat.id, f"Не могу запустить викторину: {e}")
    
    _quiz_state_ref(m.from_user.id).set(state, merge=True)
    bot.send_message(
        m.chat.id,
        _render_quiz_message(state),
        parse_mode="Markdown",
        reply_markup=_quiz_keyboard(state)
    )

@bot.message_handler(commands=['quizstats'])
def quiz_stats(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    
    snap = _quiz_stats_ref(m.from_user.id).get()
    d = snap.to_dict() if snap.exists else {"total": 0, "correct": 0}
    bot.send_message(m.chat.id, f"Твой счёт: {d.get('correct',0)}/{d.get('total',0)}")

@bot.message_handler(commands=['quizreset'])
def quiz_reset(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    
    _quiz_stats_ref(m.from_user.id).delete()
    bot.send_message(m.chat.id, "Счёт сброшен.")

@bot.message_handler(commands=['pod'])
def cmd_pod(m):
    if not is_owner(m.from_user.id):
        return bot.send_message(m.chat.id, "⛔ Нет прав")
    
    send_phrase_of_the_day_now()
    bot.send_message(m.chat.id, "Фразу дня разослала всем (кто ещё не получал сегодня).")

@bot.message_handler(commands=['fact'])
def cmd_fact(m):
    if not is_owner(m.from_user.id):
        return bot.send_message(m.chat.id, "⛔ Нет прав")
    
    send_fact_of_the_day_now()
    bot.send_message(m.chat.id, "Факт дня разослала всем (кто ещё не получал сегодня).")

@bot.message_handler(commands=['donate'])
def cmd_donate(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    
    # Bit: шлём QR фотографией
    try:
        with open(BIT_QR_IMAGE, "rb") as photo:
            bot.send_photo(
                m.chat.id,
                photo,
                caption=(
                    "☕ Поддержать через *Bit* — отсканируй QR.\n"
                    "Это добровольный донат и *не влияет* на лимиты.\n"
                    "Для безлимита есть /premium."
                ),
                parse_mode="Markdown"
            )
    except Exception:
        pass
    
    # PayBox: кнопка
    kb = InlineKeyboardMarkup()
    for title, url in DONATE_LINKS:
        kb.add(InlineKeyboardButton(text=title, url=url))
    bot.send_message(m.chat.id, "Или поддержать через PayBox 👇", reply_markup=kb)

@bot.message_handler(commands=['history'])
def cmd_history(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    
    try:
        docs = list(_history_ref(m.from_user.id).order_by("ts", direction=firestore.Query.DESCENDING).limit(5).stream())
        if not docs:
            return bot.send_message(m.chat.id, "История пуста.")
        
        lines = ["🗂 *Последние переводы:*"]
        for d in docs:
            x = d.to_dict()
            ts = x.get("ts", "")[:19].replace("T", " ")
            src = (x.get("source", "")[:120] or "").replace("\n", " ")
            res = (x.get("result", "")[:120] or "").replace("\n", " ")
            lines.append(f"• [{ts}] {src} → {res}")
        
        bot.send_message(m.chat.id, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        bot.send_message(m.chat.id, f"⚠️ Не удалось получить историю: {e}")

@bot.message_handler(commands=['stats'])
def cmd_stats(m):
    # доступ только для админов
    if int(m.from_user.id) not in ALLOWED_ADMINS:
        return bot.send_message(m.chat.id, "⛔ Доступ только для администратора.")
    
    try:
        users_ref = db.collection("users")
        # Всего пользователей (число документов в коллекции users)
        total = sum(1 for _ in users_ref.stream())
        
        # Активны сегодня (с 00:00 по Asia/Jerusalem)
        now_il = datetime.now(tz)
        start_il = now_il.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc_iso = start_il.astimezone(timezone.utc).isoformat()
        today = sum(1 for _ in users_ref.where("last_seen", ">=", start_utc_iso).stream())
        
        # Активны за 7 дней
        cutoff_utc_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        week = sum(1 for _ in users_ref.where("last_seen", ">=", cutoff_utc_iso).stream())
        
        text = (
            "📊 *Статистика бота*\n"
            f"• Всего пользователей: *{total}*\n"
            f"• Активны сегодня: *{today}*\n"
            f"• Активны за 7 дней: *{week}*"
        )
        bot.send_message(m.chat.id, text, parse_mode="Markdown")
    except Exception as e:
        bot.send_message(m.chat.id, f"⚠️ Ошибка статистики: {e}")

@bot.message_handler(commands=['premium'])
def cmd_premium(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    
    pro = is_premium(m.from_user.id)
    status = "✅ Активен" if pro else "❌ Не активен"
    msg = (
        f"⭐ *Botargem Premium*\nСтатус: {status}\n\n"
        "Цена: 15₪/мес (PayBox).\n\n"
        "После оплаты нажмите «Отправить чек» и перешлите ссылку PayBox *или* скриншот *с подписью* (сумма + дата/время)."
    )
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Оплатить в PayBox", "https://links.payboxapp.com/FqQZPo2wfWb"))
    kb.add(InlineKeyboardButton("📩 Отправить чек PayBox", callback_data="rcpt:paybox"))
    bot.send_message(m.chat.id, msg, parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(commands=['setpremium'])
def cmd_setpremium(m):
    if not is_owner(m.from_user.id):
        return bot.send_message(m.chat.id, "⛔ Нет прав")
    
    try:
        _, uid, until = m.text.split(maxsplit=2)
        uid = int(uid)
        db.collection("premium_users").document(str(uid)).set({"active": True, "until": until}, merge=True)
        bot.send_message(m.chat.id, f"✅ Премиум включён для {uid} до {until}")
        try:
            bot.send_message(uid, f"⭐ Тебе включили Premium до {until} 🙌")
        except Exception:
            pass
    except Exception as e:
        bot.send_message(m.chat.id, f"⚠️ Ошибка: {e}\nФормат: /setpremium <user_id> <YYYY-MM-DD>")

@bot.message_handler(commands=['subs', 'subscribe', 'подписка'])
def cmd_subs(m):
    _ensure_user(m.from_user)
    doc = db.collection("users").document(str(m.from_user.id)).get()
    d = doc.to_dict() or {}
    sub_pod = bool(d.get("sub_pod", True))
    sub_fact = bool(d.get("sub_fact", True))
    
    text = (
        "🔔 Управление подписками\n"
        f"• ☀️ Фраза дня: {'включена' if sub_pod else 'выключена'}\n"
        f"• 📜 Факт дня: {'включён' if sub_fact else 'выключен'}\n\n"
        "Нажми кнопку, чтобы переключить."
    )
    bot.send_message(m.chat.id, text, reply_markup=_subs_kb(sub_pod, sub_fact))

@bot.message_handler(commands=['podon', 'podoff', 'facton', 'factoff'])
def cmd_subs_short(m):
    _ensure_user(m.from_user)
    cmd = m.text.lstrip('/').lower()
    field = 'sub_pod' if 'pod' in cmd else 'sub_fact'
    val = cmd.endswith('on')
    db.collection("users").document(str(m.from_user.id)).set({field: val}, merge=True)
    tit = "Фраза дня" if field == 'sub_pod' else "Факт дня"
    bot.send_message(m.chat.id, f"✅ {tit}: {'включено' if val else 'выключено'}")

# ===== ОБРАБОТЧИКИ СООБЩЕНИЙ =====

@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    # Если ждём чек — пробуем принять как чек
    if receipt_state.get(m.chat.id):
        _accept_receipt_message(m)
        return
    
    # Не ждём чек: мягко направим в /premium
    bot.send_message(m.chat.id, "Чтобы отправить чек, нажмите /premium → «📩 Отправить чек PayBox» и следуйте инструкции.")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    _ensure_user(message.from_user)
    
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return
    
    if not contains_hebrew(message.text):
        return bot.send_message(
            message.chat.id,
            "🌸 Я перевожу *только с иврита на русский*.\n"
            "Пожалуйста, пришлите текст на иврите или голосовое на иврите.",
            parse_mode="Markdown"
        )
    
    if message.text.startswith('/'):
        return
    
    if message.forward_from or message.forward_from_chat:
        user_data[message.chat.id] = {'forwarded_text': message.text.strip()}
        bot.send_message(
            message.chat.id,
            "📩 Пересланное сообщение. Хотите перевести?",
            reply_markup=get_yes_no_keyboard()
        )
        return
    
    if receipt_state.get(message.chat.id):
        if _accept_receipt_message(message):
            return
    
    user_id = message.from_user.id
    orig = (message.text or "").strip()
    
    # Проверка на осмысленность — отсекаем «точки/эмодзи/!!!»
    if not any(ch.isalpha() for ch in orig):
        bot.send_message(message.chat.id, "🤔 Отправьте, пожалуйста, слово или фразу для перевода.")
        return
    
    # 1) по количеству
    if not can_use(user_id, "text"):
        bot.send_message(message.chat.id, limit_msg("text"), parse_mode="Markdown")
        return
    
    # 2) по символам
    ok, why = can_use_text_volume(user_id, len(orig))
    if not ok:
        bot.send_message(message.chat.id, why)
        return
    
    try:
        user_translations[message.chat.id] = orig
        translated_text = translate_text(orig)
        user_engine[message.chat.id] = "google"
        
        bot.send_message(
            message.chat.id,
            f"📘 Перевод:\n*{translated_text}*",
            reply_markup=get_keyboard(),
            parse_mode='Markdown'
        )
        add_history(message.from_user.id, "text", orig, translated_text)
    except Exception as e:
        print(f"Ошибка при переводе: {e}")
        bot.send_message(message.chat.id, "Ошибка при переводе 🫣")

@bot.message_handler(content_types=['voice', 'audio', 'document'])
def handle_voice(message):
    _ensure_user(message.from_user)
    
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return
    
    if message.forward_from or message.forward_from_chat:
        user_data[message.chat.id] = {'forwarded_audio': message}
        bot.send_message(
            message.chat.id,
            "📩 Пересланное аудио. Хотите расшифровать и перевести?",
            reply_markup=get_yes_no_keyboard()
        )
        return
    
    user_id = message.from_user.id
    
    # 1) по количеству
    if not can_use(user_id, "audio"):
        bot.send_message(message.chat.id, limit_msg("audio"), parse_mode="Markdown")
        return
    
    # длительность
    duration = 0
    if message.content_type == 'voice' and message.voice:
        duration = int(message.voice.duration or 0)
    elif message.content_type == 'audio' and message.audio:
        duration = int(message.audio.duration or 0)
    
    # 2) по секундам
    ok, why = can_use_audio_volume(user_id, duration)
    if not ok:
        bot.send_message(message.chat.id, why)
        return
    
    process_audio(message)

# ===== CALLBACK HANDLERS =====

@bot.callback_query_handler(func=lambda call: call.data.startswith("qz:"))
def cb_quiz(c):
    user_id = c.from_user.id
    if not check_access(user_id):
        return bot.answer_callback_query(c.id, "Нет доступа")
    
    data = c.data
    
    if data == "qz:stop":
        _reset_current(user_id)
        bot.answer_callback_query(c.id, "Остановлено")
        try:
            bot.edit_message_text(
                "Викторина остановлена. Возвращайся с /quiz 🙌",
                c.message.chat.id,
                c.message.message_id
            )
        except Exception:
            pass
        return
    
    if data == "qz:again":
        try:
            state = _choose_question()
        except Exception as e:
            return bot.answer_callback_query(c.id, f"Ошибка: {e}")
        
        _quiz_state_ref(user_id).set(state, merge=True)
        try:
            bot.edit_message_text(
                _render_quiz_message(state),
                c.message.chat.id,
                c.message.message_id,
                parse_mode="Markdown",
                reply_markup=_quiz_keyboard(state)
            )
        except Exception:
            bot.send_message(
                c.message.chat.id,
                _render_quiz_message(state),
                parse_mode="Markdown",
                reply_markup=_quiz_keyboard(state)
            )
        bot.answer_callback_query(c.id, "Поехали!")
        return
    
    if data.startswith("qz:pick:"):
        try:
            chosen = int(data.split(":")[2])
        except Exception:
            return bot.answer_callback_query(c.id, "Что-то пошло не так…")
        
        snap = _quiz_state_ref(user_id).get()
        if not snap.exists:
            bot.answer_callback_query(c.id, "Вопрос не найден. Жми «Ещё».")
            try:
                bot.edit_message_text(
                    "Этот вопрос уже закрыт. Жми «Ещё».",
                    c.message.chat.id,
                    c.message.message_id,
                    reply_markup=_again_keyboard()
                )
            except Exception:
                pass
            return
        
        state = snap.to_dict()
        if state.get("done"):
            bot.answer_callback_query(c.id, "Этот вопрос уже отвечён.")
            try:
                bot.edit_message_reply_markup(
                    c.message.chat.id,
                    c.message.message_id,
                    reply_markup=_again_keyboard()
                )
            except Exception:
                pass
            return
        
        opts = state["options"]
        correct_idx = int(state["answer"])
        correct = (chosen == correct_idx)
        stats = _inc_stats(user_id, correct)
        
        state["done"] = True
        _quiz_state_ref(user_id).set({"done": True}, merge=True)
        
        mark = "✅ Правильно!" if correct else "❌ Мимо."
        reveal = f"Правильный ответ: {correct_idx+1}. {opts[correct_idx]}"
        note = state.get("note") or ""
        score = f"Счёт: {stats.get('correct',0)}/{stats.get('total',0)}"
        
        text = [
            "🧠 *Викторина* — результат",
            f"🗣 {state['he']}",
            "",
            f"Ты выбрал: {chosen+1}. {opts[chosen]}",
            f"{mark} {reveal}"
        ]
        
        if note:
            text.append(f"💬 Пояснение: {note}")
        
        text += ["", score, "Хочешь ещё?"]
        final = "\n".join(text)
        
        bot.answer_callback_query(c.id, "Принято!")
        try:
            bot.edit_message_text(
                final,
                c.message.chat.id,
                c.message.message_id,
                parse_mode="Markdown",
                reply_markup=_again_keyboard()
            )
        except Exception:
            bot.send_message(
                c.message.chat.id,
                final,
                parse_mode="Markdown",
                reply_markup=_again_keyboard()
            )

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not check_access(call.from_user.id):
        return bot.answer_callback_query(call.id, "Нет доступа")
    
    bot.answer_callback_query(call.id)
    
    # --- Чеки / квитанции ---
    if call.data == "rcpt:paybox":
        receipt_state[call.message.chat.id] = {
            "provider": "paybox",
            "ts": datetime.utcnow().isoformat()
        }
        bot.send_message(
            call.message.chat.id,
            "🔎 Отправьте, пожалуйста, *ссылку PayBox* на оплату ИЛИ *скриншот*.\n"
            "Если отправляете скрин — добавьте подпись: *сумма* и *дата/время*.\n\n"
            "Пример подписи: 15₪, 02.09 10:35",
            parse_mode="Markdown"
        )
        return
    
    # --- Подписки: обработка кнопок ---
    if call.data.startswith("subs:"):
        try:
            _, kind, action = call.data.split(":")  # kind in {"pod","fact"}, action in {"on","off"}
            field = "sub_pod" if kind == "pod" else "sub_fact"
            val = (action == "on")
            uid = str(call.from_user.id)
            
            db.collection("users").document(uid).set({field: val}, merge=True)
            
            doc = db.collection("users").document(uid).get()
            d = doc.to_dict() or {}
            sub_pod = bool(d.get("sub_pod", True))
            sub_fact = bool(d.get("sub_fact", True))
            
            txt = (
                "🔔 Подписки обновлены\n"
                f"• ☀️ Фраза дня: {'включена' if sub_pod else 'выключена'}\n"
                f"• 📜 Факт дня: {'включён' if sub_fact else 'выключен'}"
            )
            
            try:
                bot.edit_message_text(
                    txt,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=_subs_kb(sub_pod, sub_fact)
                )
            except Exception:
                bot.send_message(
                    call.message.chat.id,
                    txt,
                    reply_markup=_subs_kb(sub_pod, sub_fact)
                )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"⚠️ Не удалось обновить подписку: {e}")
        return
    
    if call.data == "explain":
        text = user_translations.get(call.message.chat.id)
        if not text:
            bot.send_message(call.message.chat.id, "Нет текста для объяснения.")
            return
        
        sys_prompt = (
            "Ты — опытный преподаватель разговорного иврита. "
            "Проанализируй фразу на иврите: переведи естественно, выдели корень, биньян, "
            "грамматическую форму глаголов; объясни сленг/идиомы и происхождение; "
            "дай короткий пример использования."
        )
        
        try:
            answer = ask_gpt(
                [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}],
                model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            )
            if answer is None:
                local = explain_local(text)
                _send_explanation_guard(call.message.chat.id, local, offline=True)
            else:
                _send_explanation_guard(call.message.chat.id, answer, offline=False)
        except AuthenticationError:
            bot.send_message(call.message.chat.id, "⚠️ Проблема с ключом OpenAI. Проверь OPENAI_API_KEY.")
        except BadRequestError as e:
            print(f"[ask_gpt] BadRequest: {e}")
            bot.send_message(call.message.chat.id, "⚠️ Не удалось разобрать запрос для объяснения.")
        except Exception as e:
            print(f"Неожиданная ошибка при объяснении: {e}")
            local = explain_local(text)
            bot.send_message(call.message.chat.id, f"🧠 Объяснение (офлайн):\n{local}")
    
    elif call.data == "new":
        chat_id = call.message.chat.id
        text = user_translations.get(chat_id)
        if not text:
            bot.send_message(chat_id, "Сначала пришли фразу, а потом жми «Ещё перевод».")
            return
        
        prev = user_engine.get(chat_id, "google")
        next_engine = "mymemory" if prev == "google" else "google"
        tr, used = translate_with_engine(text, next_engine)
        user_engine[chat_id] = used
        
        engine_title = "MyMemory" if used == "mymemory" else "Google"
        bot.send_message(
            chat_id,
            f"📘 Вариант ({engine_title}):\n*{tr}*",
            reply_markup=get_keyboard(),
            parse_mode='Markdown'
        )
    
    elif call.data == "translate_forwarded":
        chat_data = user_data.get(call.message.chat.id, {})
        
        if 'forwarded_text' in chat_data:
            text = chat_data['forwarded_text']
            user_translations[call.message.chat.id] = text
            translated_text = translate_text(text)
            bot.send_message(
                call.message.chat.id,
                f"📘 Перевод:\n*{translated_text}*",
                reply_markup=get_keyboard(),
                parse_mode='Markdown'
            )
        elif 'forwarded_audio' in chat_data:
            process_audio(chat_data['forwarded_audio'])
        
        if call.message.chat.id in user_data:
            del user_data[call.message.chat.id]
    
    elif call.data == "cancel":
        bot.send_message(call.message.chat.id, "❌ Отменено")
        if call.message.chat.id in user_data:
            del user_data[call.message.chat.id]

# ===== GRACEFUL SHUTDOWN =====
def signal_handler(sig, frame):
    print('\n🛑 Получен сигнал завершения. Останавливаю бота...')
    bot.stop_polling()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ===== ЗАПУСК =====
print("🚀 Botargem запущен с защитой от дублей ✅")

if __name__ == "__main__":
    while True:
        try:
            print("⏳ Запускаю infinity_polling...")
            bot.infinity_polling(
                timeout=20,
                long_polling_timeout=20,
                skip_pending=True,
                allowed_updates=['message', 'callback_query']
            )
        except telebot.apihelper.ApiTelegramException as e:
            s = str(e)
            if "409" in s or "getUpdates request" in s:
                print("⚠️ 409: другой процесс ещё поллит. Жду 25 сек и пробую снова…")
                time.sleep(25)
                continue
            print(f"Telebot error: {e}. Повтор через 10 сек.")
            time.sleep(10)
            continue
        except Exception as e:
            print(f"Критическая ошибка: {e}. Повтор через 10 сек.")
            time.sleep(10)