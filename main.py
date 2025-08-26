# --- ИМПОРТЫ (коротко и без дублей) ---
import os, sys, time, threading, signal, random, re, json, hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer

import openai  # для openai.api_key = ...
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
TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
# Мини-диагностика, чтобы в логах сразу было видно, что именно пришло
def _mask_token(t: str) -> str:
    if not t:
        return "<empty>"
    head = t.split(":")[0]  # обычно цифры до двоеточия
    tail = t[-4:] if len(t) >= 4 else t
    return f"{head}:***...***{tail}"

if not TOKEN or ":" not in TOKEN:
    print(f"[BOOT] TELEGRAM_BOT_TOKEN invalid. Read='{_mask_token(TOKEN)}' len={len(TOKEN)}")
    raise RuntimeError("TELEGRAM_BOT_TOKEN отсутствует или неверен. Проверь в Render → Settings → Environment.")
else:
    print(f"[BOOT] TELEGRAM_BOT_TOKEN ok: {_mask_token(TOKEN)}")
# Если у тебя токен жёстко вшит — можешь заменить на строку:
# TOKEN = "8147...cvFU"

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

# ===== Пользователи (ограничение доступа) =====
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
    return not ALLOWED_USERS or user_id in ALLOWED_USERS

@bot.message_handler(commands=['id'])
def send_user_id(message):
    bot.send_message(message.chat.id, f"👤 Твой Telegram ID: `{message.from_user.id}`", parse_mode='Markdown')

# ===== ФРАЗА ДНЯ (одна реализация) =====
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
    send_phrase_of_the_day_now()
    bot.send_message(m.chat.id, "Фразу дня разослала всем (кто ещё не получал сегодня).")

# ===== ВИКТОРИНА (минимальная, из наших патчей) =====
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
    try:
        state = _choose_question()
    except Exception as e:
        return bot.send_message(m.chat.id, f"Не могу запустить викторину: {e}")
    _quiz_state_ref(m.from_user.id).set(state, merge=True)
    bot.send_message(m.chat.id, _render_quiz_message(state), parse_mode="Markdown", reply_markup=_quiz_keyboard(state))

@bot.message_handler(commands=['quizstats'])
def quiz_stats(m):
    snap = _quiz_stats_ref(m.from_user.id).get()
    d = snap.to_dict() if snap.exists else {"total": 0, "correct": 0}
    bot.send_message(m.chat.id, f"Твой счёт: {d.get('correct',0)}/{d.get('total',0)}")

@bot.message_handler(commands=['quizreset'])
def quiz_reset(m):
    _quiz_stats_ref(m.from_user.id).delete()
    bot.send_message(m.chat.id, "Счёт сброшен.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("qz:"))
def cb_quiz(c):
    user_id = c.from_user.id; data = c.data
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

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return
    if message.forward_from or message.forward_from_chat:
        user_data[message.chat.id] = {'forwarded_text': message.text.strip()}
        bot.send_message(message.chat.id, "📩 Пересланное сообщение. Хотите перевести?", reply_markup=get_yes_no_keyboard())
        return
    try:
        orig = message.text.strip()
        user_translations[message.chat.id] = orig
        translated_text = translate_text(orig)
        bot.send_message(message.chat.id, f"📘 Перевод:\n*{translated_text}*", reply_markup=get_keyboard(), parse_mode='Markdown')
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
    process_audio(message)

def process_audio(message):
    try:
        file_info = bot.get_file(message.voice.file_id if message.content_type == 'voice' else message.audio.file_id)
        data = bot.download_file(file_info.file_path)
        tmp_path = "voice.ogg"
        with open(tmp_path, "wb") as f:
            f.write(data)
        with open(tmp_path, "rb") as audio_file:
            try:
                transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file, language="he")
            except Exception as api_err:
                if "overloaded" in str(api_err).lower():
                    bot.send_message(message.chat.id, "🤖 Сейчас сервер перегружен, попробуй чуть позже.")
                else:
                    bot.send_message(message.chat.id, "⚠️ Ошибка при расшифровке аудио.")
                return
        hebrew_text = transcript.text
        translated_text = translate_text(hebrew_text)
        user_translations[message.chat.id] = hebrew_text
        bot.send_message(message.chat.id, f"🗣 Распознанный текст:\n_{hebrew_text}_\n\n📘 Перевод:\n*{translated_text}*", parse_mode='Markdown', reply_markup=get_keyboard())
    except Exception as e:
        print(f"Ошибка с аудио: {e}")
        bot.send_message(message.chat.id, "Не удалось обработать аудио 😢")
    finally:
        if os.path.exists("voice.ogg"):
            os.remove("voice.ogg")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
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
        text = user_translations.get(call.message.chat.id)
        if text:
            try:
                translated_text = translate_text(text)
                bot.send_message(call.message.chat.id, f"📘 Новый перевод:\n*{translated_text}*", reply_markup=get_keyboard(), parse_mode='Markdown')
            except Exception as e:
                print(f"Ошибка повторного перевода: {e}")
                bot.send_message(call.message.chat.id, "Ошибка при переводе 🫣")
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
print("🚀 AhlaBot запущен с защитой от дублей ✅")

def ask_gpt(messages, model="gpt-4o", max_retries=3):
    """Запрос к OpenAI с ретраями и экспоненциальной паузой."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=0.4, timeout=30,
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
    try:
        print("⏳ Запускаю infinity_polling...")
        bot.infinity_polling(timeout=20, long_polling_timeout=20,
                             skip_pending=True,
                             allowed_updates=['message','callback_query'])
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        sys.exit(1)
