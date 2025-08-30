# --- ИМПОРТЫ (коротко и без дублей) ---
import os, sys, time, threading, signal, random, re, json, hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer

import openai
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import firebase_admin
from firebase_admin import credentials, firestore
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ===== БАЗОВЫЕ НАСТРОЙКИ =====
load_dotenv()
HEB_RE = re.compile(r'[\u0590-\u05FF]')
tz = pytz.timezone('Asia/Jerusalem')

# OpenAI (старый стиль ключа + новый клиент)
openai.api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
from openai import (
    OpenAI, APIConnectionError, RateLimitError, APIStatusError,
    AuthenticationError, BadRequestError,
)
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
        try: self.wfile.write(b"ok")
        except BrokenPipeError: pass
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

bot = create_bot_with_retry()
VERSION = "botargem-3"

@bot.message_handler(commands=['version'])
def cmd_version(m):
    bot.send_message(m.chat.id, f"Версия кода: {VERSION}")

# Версия бота (для проверки деплоя)
VERSION = "botargem-1"

@bot.message_handler(commands=['version'])
def cmd_version(m):
    bot.send_message(m.chat.id, f"Версия кода: {VERSION}")

# какой движок перевода использовали в последний раз для этого чата
user_engine = {}  # chat_id -> "google" | "mymemory"

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

# ---- офлайн-фолбэк для "Объяснить" ----
IDOMS = {
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
    low = he_text.replace("׳","").replace("'","").replace("`","")
    for k, note in IDOMS.items():
        if k in low or k.replace("׳","") in low:
            hits.append(f"• *{k}* — {note}")
    note_block = "\n".join(hits) if hits else "Сленг/идиом не найдено."
    return (
        f"Перевод: {tr}\n\n"
        f"Сленг/идиомы:\n{note_block}\n\n"
        f"Грамматика: разговорная речь; для точного морфоразбора нужен онлайн-режим."
    )

# ===== Firebase (ИДЕМПОТЕНТНО!) =====
def _find_firebase_key():
    candidates = []
    if os.getenv("FIREBASE_CREDENTIALS_PATH"):
        candidates.append(os.getenv("FIREBASE_CREDENTIALS_PATH"))
    repo_file = os.path.join(os.path.dirname(__file__),
                             "trivia-game-79e1b-firebase-adminsdk-fbsvc-20be34c499.json")
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

def check_access(user_id:int) -> bool:
    return bool(ALLOWED_USERS) and (user_id in ALLOWED_USERS)

# ===== Админ: владелец (только ты) =====
# можно задать OWNER_ID через переменную окружения; иначе возьмём «первого» из allowed_users
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
if not OWNER_ID and ALLOWED_USERS:
    OWNER_ID = sorted(ALLOWED_USERS)[0]
print(f"👑 OWNER_ID = {OWNER_ID or 'не задан'}")

def is_owner(user_id:int) -> bool:
    return OWNER_ID and (user_id == OWNER_ID)

# ===== ЛИМИТЫ / ПРЕМИУМ / ДОНАТЫ =====
FREE_LIMIT_TEXT = 3
FREE_LIMIT_AUDIO = 3

TEXT_MAX_LEN_PER_MSG = 500
TEXT_MAX_LEN_PER_DAY = 1500
AUDIO_MAX_SEC_PER_MSG = 60
AUDIO_MAX_SEC_PER_DAY = 180

TEXT_TOO_LONG_MSG = (f"⚠️ Сообщение слишком длинное. Максимум {TEXT_MAX_LEN_PER_MSG} символов за раз. Разбей на части 🙂")
AUDIO_TOO_LONG_MSG = (f"⚠️ Голосовое слишком длинное. Максимум {AUDIO_MAX_SEC_PER_MSG} секунд за раз. Попробуй короче 🙂")

DONATE_LINKS = [
    ("🍰 PayBox", "https://links.payboxapp.com/FqQZPo2wfWb"),
]

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
def is_premium(user_id:int) -> bool:
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

def can_use_text_volume(user_id: int, msg_len: int) -> (bool, str):
    if is_premium(user_id):
        if msg_len > 2000:
            return False, "⚠️ Очень длинное сообщение. Разбей, пожалуйста."
        return True, ""
    if msg_len > TEXT_MAX_LEN_PER_MSG:
        return False, TEXT_TOO_LONG_MSG
    d = get_usage(user_id)
    if d["text_chars"] + msg_len > TEXT_MAX_LEN_PER_DAY:
        left = max(0, TEXT_MAX_LEN_PER_DAY - d["text_chars"])
        return False, (f"🚫 Лимит символов на сегодня исчерпан. Осталось: {left}/{TEXT_MAX_LEN_PER_DAY}. Завтра обнулится.")
    d["text_chars"] += msg_len
    save_usage(user_id, d)
    return True, ""

def can_use_audio_volume(user_id: int, duration_sec: int) -> (bool, str):
    if is_premium(user_id):
        if duration_sec > 600:
            return False, "⚠️ Очень длинное аудио. Сделай короче, пожалуйста."
        return True, ""
    if duration_sec > AUDIO_MAX_SEC_PER_MSG:
        return False, AUDIO_TOO_LONG_MSG
    d = get_usage(user_id)
    if d["audio_secs"] + duration_sec > AUDIO_MAX_SEC_PER_DAY:
        left = max(0, AUDIO_MAX_SEC_PER_DAY - d["audio_secs"])
        return False, (f"🚫 Лимит длительности аудио исчерпан. Осталось: {left} сек. из {AUDIO_MAX_SEC_PER_DAY}. Завтра обнулится.")
    d["audio_secs"] += duration_sec
    save_usage(user_id, d)
    return True, ""

def limit_msg(kind):
    if kind == "text":
        return "🚫 Лимит *текстовых* переводов (3) исчерпан. 🔄 Сброс в полночь. Нужен безлимит? /premium"
    else:
        return "🚫 Лимит *аудио* переводов (3) исчерпан. 🔄 Сброс в полночь. Нужен безлимит? /premium"

# ===== ПОЛЕЗНОЕ: /id =====
@bot.message_handler(commands=['id'])
def send_user_id(message):
    bot.send_message(message.chat.id, f"👤 Твой Telegram ID: `{message.from_user.id}`", parse_mode='Markdown')

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
        f"💬 Пояснение: {item.get('note','—')}"
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
    recipients = ALLOWED_USERS
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

@bot.message_handler(commands=['pod'])
def cmd_pod(m):
    if not is_owner(m.from_user.id):  # только ты
        return bot.send_message(m.chat.id, "⛔ Нет прав")
    send_phrase_of_the_day_now()
    bot.send_message(m.chat.id, "Фразу дня разослала всем (кто ещё не получал сегодня).")

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
            return {"he": d.get("he",""), "ru": d.get("ru",""), "note": d.get("note","")}
    except Exception as e:
        print(f"[facts] FS err: {e}")
    return random.choice(FACTS_DB)

def build_fact_message(item):
    msg = f"📜 *Факт дня*\n\n🗣 {item.get('he','')}\n📘 Перевод: {item.get('ru','')}"
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
    recipients = ALLOWED_USERS
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

@bot.message_handler(commands=['fact'])
def cmd_fact(m):
    if not is_owner(m.from_user.id):  # только ты
        return bot.send_message(m.chat.id, "⛔ Нет прав")
    send_fact_of_the_day_now()
    bot.send_message(m.chat.id, "Факт дня разослала всем (кто ещё не получал сегодня).")

# ===== ВИКТОРИНА (как раньше) =====
QUIZ_COLL = "quiz"
QUIZ_DOC  = "current"
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
            seen.add(v); distractors.append(v)
        if len(distractors) >= k: break
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
    return {"he": he, "ru": ru, "note": note, "options": options, "answer": answer_idx, "ts": datetime.utcnow().isoformat(), "done": False}

def _render_quiz_message(state):
    he = state["he"]; opts = state["options"]
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
    if correct: d["correct"] = int(d.get("correct", 0)) + 1
    ref.set(d, merge=True)
    return d

def _reset_current(user_id):
    _quiz_state_ref(user_id).delete()

@bot.message_handler(commands=['quiz'])
def cmd_quiz(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    try:
        state = _choose_question()
    except Exception as e:
        return bot.send_message(m.chat.id, f"Не могу запустить викторину: {e}")
    _quiz_state_ref(m.from_user.id).set(state, merge=True)
    bot.send_message(m.chat.id, _render_quiz_message(state), parse_mode="Markdown", reply_markup=_quiz_keyboard(state))

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

@bot.callback_query_handler(func=lambda c: c.data.startswith("qz:"))
def cb_quiz(c):
    user_id = c.from_user.id
    if not check_access(user_id):
        return bot.answer_callback_query(c.id, "Нет доступа")
    data = c.data
    if data == "qz:stop":
        _reset_current(user_id)
        bot.answer_callback_query(c.id, "Остановлено")
        try:
            bot.edit_message_text("Викторина остановлена. Возвращайся с /quiz 🙌", c.message.chat.id, c.message.message_id)
        except Exception: pass
        return
    if data == "qz:again":
        try: state = _choose_question()
        except Exception as e: return bot.answer_callback_query(c.id, f"Ошибка: {e}")
        _quiz_state_ref(user_id).set(state, merge=True)
        try:
            bot.edit_message_text(_render_quiz_message(state), c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=_quiz_keyboard(state))
        except Exception:
            bot.send_message(c.message.chat.id, _render_quiz_message(state), parse_mode="Markdown", reply_markup=_quiz_keyboard(state))
        bot.answer_callback_query(c.id, "Поехали!")
        return
    if data.startswith("qz:pick:"):
        try: chosen = int(data.split(":")[2])
        except Exception: return bot.answer_callback_query(c.id, "Что-то пошло не так…")
        snap = _quiz_state_ref(user_id).get()
        if not snap.exists:
            bot.answer_callback_query(c.id, "Вопрос не найден. Жми «Ещё».")
            try: bot.edit_message_text("Этот вопрос уже закрыт. Жми «Ещё».", c.message.chat.id, c.message.message_id, reply_markup=_again_keyboard())
            except Exception: pass
            return
        state = snap.to_dict()
        if state.get("done"):
            bot.answer_callback_query(c.id, "Этот вопрос уже отвечён.")
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=_again_keyboard())
            except Exception: pass
            return
        opts = state["options"]; correct_idx = int(state["answer"])
        correct = (chosen == correct_idx); stats = _inc_stats(user_id, correct)
        state["done"] = True; _quiz_state_ref(user_id).set({"done": True}, merge=True)
        mark = "✅ Правильно!" if correct else "❌ Мимо."
        reveal = f"Правильный ответ: {correct_idx+1}. {opts[correct_idx]}"
        note = state.get("note") or ""; score = f"Счёт: {stats.get('correct',0)}/{stats.get('total',0)}"
        text = ["🧠 *Викторина* — результат", f"🗣 {state['he']}", "", f"Ты выбрал: {chosen+1}. {opts[chosen]}", f"{mark} {reveal}"]
        if note: text.append(f"💬 Пояснение: {note}")
        text += ["", score, "Хочешь ещё?"]
        final = "\n".join(text)
        bot.answer_callback_query(c.id, "Принято!")
        try:
            bot.edit_message_text(final, c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=_again_keyboard())
        except Exception:
            bot.send_message(c.message.chat.id, final, parse_mode="Markdown", reply_markup=_again_keyboard())

# ===== Донаты =====
@bot.message_handler(commands=['donate'])
def cmd_donate(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    # Bit: шлём QR фотографией (положи файл рядом с кодом; назови bit_qr.jpg)
    try:
        with open("bit_qr.jpg", "rb") as photo:
            bot.send_photo(
                m.chat.id, photo,
                caption=("☕ Поддержать через *Bit* — отсканируй QR.\n"
                         "Это добровольный донат и *не влияет* на лимиты.\n"
                         "Для безлимита есть /premium."),
                parse_mode="Markdown"
            )
    except Exception:
        pass
    # PayBox: кнопка
    kb = InlineKeyboardMarkup()
    for title, url in DONATE_LINKS:
        kb.add(InlineKeyboardButton(text=title, url=url))
    bot.send_message(m.chat.id, "Или поддержать через PayBox 👇", reply_markup=kb)

# ===== История переводов =====
def _history_ref(user_id: int):
    return db.collection("users").document(str(user_id)).collection("history")

def add_history(user_id:int, kind:str, source:str, result:str):
    try:
        _history_ref(user_id).add({
            "ts": datetime.utcnow().isoformat(),
            "kind": kind,          # "text" | "audio"
            "source": (source or "")[:4000],
            "result": (result or "")[:4000],
        })
    except Exception as e:
        print("[history] err:", e)

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
            ts = x.get("ts","")[:19].replace("T"," ")
            src = (x.get("source","")[:120] or "").replace("\n"," ")
            res = (x.get("result","")[:120] or "").replace("\n"," ")
            lines.append(f"• [{ts}] {src} → {res}")
        bot.send_message(m.chat.id, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        bot.send_message(m.chat.id, f"⚠️ Не удалось получить историю: {e}")

# ===== PREMIUM команды (админские действия — только OWNER) =====
@bot.message_handler(commands=['premium'])
def cmd_premium(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    pro = is_premium(m.from_user.id)
    status = "✅ Активен" if pro else "❌ Не активен"
    msg = (
        f"⭐ *Botargem Premium*\n"
        f"Статус: {status}\n\n"
        "Что даёт:\n"
        "• Без лимитов\n"
        "• Длиннее сообщения и аудио\n\n"
        "Цена: 15₪/мес (PayBox).\n"
        "После оплаты пришлите чек фотографией прямо сюда — бот отправит его администратору."
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Оплатить в PayBox", "https://links.payboxapp.com/FqQZPo2wfWb"))
    bot.send_message(m.chat.id, msg, parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(content_types=['photo'])
def handle_check(m):
    if m.chat.type != "private":
        return
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
    uid = m.from_user.id
    uname = m.from_user.username or "—"
    # переслать чек только владельцу
    if OWNER_ID:
        try:
            bot.forward_message(OWNER_ID, m.chat.id, m.message_id)
            bot.send_message(OWNER_ID, f"📩 Чек на премиум\nID: {uid}\nUsername: @{uname}")
        except Exception as e:
            print(f"[check->owner {OWNER_ID}] err:", e)
    bot.send_message(m.chat.id, "✅ Спасибо! Чек отправлен администратору. Премиум активируем в течение суток.")

@bot.message_handler(commands=['setpremium'])
def cmd_setpremium(m):
    if not is_owner(m.from_user.id):
        return bot.send_message(m.chat.id, "⛔ Нет прав")
    try:
        _, uid, until = m.text.split(maxsplit=2)
        uid = int(uid)
        db.collection("premium_users").document(str(uid)).set({"active": True, "until": until}, merge=True)
        bot.send_message(m.chat.id, f"✅ Премиум включён для {uid} до {until}")
        try: bot.send_message(uid, f"⭐ Тебе включили Premium до {until} 🙌")
        except Exception: pass
    except Exception as e:
        bot.send_message(m.chat.id, f"⚠️ Ошибка: {e}\nФормат: /setpremium <user_id> <YYYY-MM-DD>")

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

# ===== Обработчики сообщений =====
user_translations = {}
user_data = {}
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

@bot.message_handler(commands=['start','help'])
def cmd_start(m):
    if not check_access(m.from_user.id):
        return bot.send_message(m.chat.id, "Извини, доступ ограничен 👮‍♀️")
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
        "• Лимиты смотри: /profile",
        reply_markup=kb
    )

# ===== Профиль / лимиты =====
def _fmt_bar(used: int, total: int, size: int = 10) -> str:
    if total <= 0: return "—"
    filled = int(round(size * min(used, total) / total))
    return "█" * filled + "░" * (size - filled)

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
    bar_t  = _fmt_bar(t_used,  t_total)
    bar_a  = _fmt_bar(a_used,  a_total)
    bar_tc = _fmt_bar(tc_used, tc_total)
    bar_as = _fmt_bar(as_used, as_total)
    now = datetime.now(tz); midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
    left = max(0, int((midnight - now).total_seconds())); hh, mm = left//3600, (left%3600)//60
    msg = (
        "👤 *Твой профиль / лимиты на сегодня*\n\n"
        f"📝 Тексты: {t_used}/{t_total}  {bar_t}\n"
        f"🔊 Аудио:  {a_used}/{a_total}  {bar_a}\n"
        f"🔡 Символы: {tc_used}/{tc_total}  {bar_tc}\n"
        f"⏱ Секунды: {as_used}/{as_total}  {bar_as}\n\n"
        f"🔄 Сброс ~через {hh}ч {mm}м (Asia/Jerusalem)"
    )
    bot.send_message(m.chat.id, msg, parse_mode="Markdown")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return
    if message.text.startswith('/'):
        return
    if message.forward_from or message.forward_from_chat:
        user_data[message.chat.id] = {'forwarded_text': message.text.strip()}
        bot.send_message(message.chat.id, "📩 Пересланное сообщение. Хотите перевести?", reply_markup=get_yes_no_keyboard())
        return

    user_id = message.from_user.id
    orig = (message.text or "").strip()

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
        bot.send_message(message.chat.id, f"📘 Перевод:\n*{translated_text}*", reply_markup=get_keyboard(), parse_mode='Markdown')
        add_history(message.from_user.id, "text", orig, translated_text)
    except Exception as e:
        print(f"Ошибка при переводе: {e}")
        bot.send_message(message.chat.id, "Ошибка при переводе 🫣")

@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message):
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return
    if message.forward_from or message.forward_from_chat:
        user_data[message.chat.id] = {'forwarded_audio': message}
        bot.send_message(message.chat.id, "📩 Пересланное аудио. Хотите расшифровать и перевести?", reply_markup=get_yes_no_keyboard())
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

def process_audio(message):
    try:
        file_id = message.voice.file_id if message.content_type == 'voice' else message.audio.file_id
        file_info = bot.get_file(file_id)
        data = bot.download_file(file_info.file_path)
        tmp_path = "voice.ogg"
        with open(tmp_path, "wb") as f:
            f.write(data)

        with open(tmp_path, "rb") as audio_file:
            try:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1", file=audio_file, language="he"
                )
            except Exception as api_err:
                if "overloaded" in str(api_err).lower():
                    bot.send_message(message.chat.id, "🤖 Сейчас сервер перегружен, попробуй чуть позже.")
                else:
                    bot.send_message(message.chat.id, "⚠️ Ошибка при расшифровке аудио.")
                return

        hebrew_text = transcript.text
        translated_text = translate_text(hebrew_text)
        user_translations[message.chat.id] = hebrew_text
        bot.send_message(
            message.chat.id,
            f"🗣 Распознанный текст:\n_{hebrew_text}_\n\n📘 Перевод:\n*{translated_text}*",
            parse_mode='Markdown',
            reply_markup=get_keyboard()
        )
        add_history(message.from_user.id, "audio", hebrew_text, translated_text)
    except Exception as e:
        print(f"Ошибка с аудио: {e}")
        bot.send_message(message.chat.id, "Не удалось обработать аудио 😢")
    finally:
        if os.path.exists("voice.ogg"):
            os.remove("voice.ogg")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not check_access(call.from_user.id):
        return bot.answer_callback_query(call.id, "Нет доступа")
    bot.answer_callback_query(call.id)
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
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": text}],
                model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            )
            if answer is None:
                local = explain_local(text)
                bot.send_message(call.message.chat.id, f"🧠 Объяснение (офлайн):\n{local}")
            else:
                bot.send_message(call.message.chat.id, f"🧠 Объяснение:\n{answer}")
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
        bot.send_message(chat_id, f"📘 Вариант ({engine_title}):\n*{tr}*", reply_markup=get_keyboard(), parse_mode='Markdown')
    elif call.data == "translate_forwarded":
        chat_data = user_data.get(call.message.chat.id, {})
        if 'forwarded_text' in chat_data:
            text = chat_data['forwarded_text']
            user_translations[call.message.chat.id] = text
            translated_text = translate_text(text)
            bot.send_message(call.message.chat.id, f"📘 Перевод:\n*{translated_text}*", reply_markup=get_keyboard(), parse_mode='Markdown')
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

def ask_gpt(messages, model="gpt-4o", max_retries=3):
    """Запрос к OpenAI с ретраями и экспоненциальной паузой."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=0.4, timeout=30,
                max_tokens=300
            )
            return resp.choices[0].message.content.strip()
        except (APIConnectionError, RateLimitError, APIStatusError) as e:
            print(f"[ask_gpt] API error (попытка {attempt}/{max_retries}): {e}")
            if attempt == max_retries: return None
        except (AuthenticationError, BadRequestError) as e:
            print(f"[ask_gpt] Auth/BadRequest error: {e}"); raise
        except Exception as e:
            print(f"[ask_gpt] Unexpected error (попытка {attempt}/{max_retries}): {e}")
            if attempt == max_retries: return None
        if attempt < max_retries:
            sleep_time = delay + random.uniform(0, 0.5)
            print(f"[ask_gpt] Ждём {sleep_time:.1f} секунд перед повтором...")
            time.sleep(sleep_time); delay *= 2
    return None

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
