# --- ИМПОРТЫ (единый и без дублей) ---
import os, sys, time, threading, signal, random
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import pytz
from datetime import datetime, timedelta

from openai import OpenAI
from openai import (
    APIConnectionError, RateLimitError, APIStatusError,
    AuthenticationError, BadRequestError,
)

# Таймзона (нужна для расписания 08:00)
tz = pytz.timezone('Asia/Jerusalem')

# Клиент OpenAI (после импортов)
client = OpenAI(api_key=(os.getenv("OPENAI_API_KEY") or "").strip(), timeout=20)

def ask_gpt(messages, model="gpt-4o", max_retries=3):
    """Запрос к OpenAI с ретраями и экспоненциальной паузой."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.4,
                timeout=30,  # добавляем таймаут
            )
            return resp.choices[0].message.content.strip()
        except (APIConnectionError, RateLimitError, APIStatusError) as e:
            print(f"[ask_gpt] API error (попытка {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                # Возвращаем None вместо исключения
                return None
        except (AuthenticationError, BadRequestError) as e:
            # это уже не сеть — ключ/запрос. Пробрасываем дальше
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

# Запускаем в отдельном потоке
threading.Thread(target=run_health_server, daemon=True).start()


# ======= НАСТРОЙКИ =======
TOKEN = '8147753305:AAEbWrC9D1hWM2xtK5L87XIGkD9GZAYcvFU'
openai.api_key = os.getenv("OPENAI_API_KEY")

# 🔒 ЗАЩИТА ОТ МНОЖЕСТВЕННЫХ ЭКЗЕМПЛЯРОВ
def clear_webhook_and_wait():
    """Очищает webhook и ждет, пока предыдущий экземпляр завершится"""
    try:
        import requests
        url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"
        response = requests.post(url)
        print(f"Webhook cleared: {response.json()}")
        
        # Ждем 10 секунд для завершения других экземпляров
        print("⏳ Ждем завершения других экземпляров...")
        time.sleep(20)
        
    except Exception as e:
        print(f"Ошибка при очистке webhook: {e}")

# Очищаем webhook при запуске
clear_webhook_and_wait()

# Создаем бота с retry логикой
def create_bot_with_retry():
    max_retries = 3
    for attempt in range(max_retries):
        try:
            bot = telebot.TeleBot(TOKEN)
            # Тестовый запрос для проверки
            bot.get_me()
            print(f"✅ Бот успешно инициализирован (попытка {attempt + 1})")
            return bot
        except telebot.apihelper.ApiTelegramException as e:
            if "409" in str(e):
                print(f"❌ Конфликт экземпляров (попытка {attempt + 1}). Жду...")
                time.sleep(15)  # Ждем дольше
            else:
                raise e
        except Exception as e:
            print(f"Ошибка инициализации бота: {e}")
            if attempt == max_retries - 1:
                raise e
            time.sleep(5)
    
    raise Exception("Не удалось создать бота после всех попыток")

bot = create_bot_with_retry()

# Глобальные словари
user_translations = {}
user_data = {}
saved_translations = {}
saved_explanations = {}

from deep_translator import GoogleTranslator, MyMemoryTranslator

def translate_text(text: str) -> str:
    """Стабильный перевод: сначала deep-translator, при ошибке — MyMemory."""
    # если есть ивритские буквы → явно ставим код языка "iw"
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
    "יאללה": "Сленг: «давай/погнали/ну же». Многофункциональное слово.",
    "סבבה": "Сленг: «окей, супер, норм». Универсальное согласие.",
    "באסה": "Сленг: «облом, неприятность».",
    "תכלס": "Сленг: «по сути, по факту». Пишут и как תכל׳ס.",
    "כפרה": "Ласковое обращение: «душа моя». Может быть и в шутку.",
    "אין מצב": "«Ни за что / да ну!» — удивление или отказ.",
    "די נו": "«Хватит уже / да ну». Лёгкое раздражение.",
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
        f"Грамматика: разговорная речь; для точного морфоразбора (корни/биньяны) нужен онлайн-режим."
    )


# ======= Firebase =======
from dotenv import load_dotenv
load_dotenv()

def _find_firebase_key():
    candidates = []
    env_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
    if env_path:
        candidates.append(env_path)

    repo_file = os.path.join(
        os.path.dirname(__file__),
        "trivia-game-79e1b-firebase-adminsdk-fbsvc-20be34c499.json"
    )
    candidates.append(repo_file)
    candidates.append("/etc/secrets/firebase-key.json")

    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError("Не найден ключ Firebase")

firebase_key_path = _find_firebase_key()
cred = credentials.Certificate(firebase_key_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ======= Пользователи =======
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

@bot.message_handler(commands=['id'])
def send_user_id(message):
    bot.send_message(message.chat.id, f"👤 Твой Telegram ID: `{message.from_user.id}`", parse_mode='Markdown')

# ======= ФРАЗЫ ДНЯ (из файла + одна фраза на день) =======
import json, os, hashlib
@bot.message_handler(commands=['quizstats'])
def quiz_stats(m):
    snap = _quiz_stats_ref(m.from_user.id).get()
    d = snap.to_dict() if snap.exists else {"total": 0, "correct": 0}
    bot.send_message(m.chat.id, f"Твой счёт: {d.get('correct',0)}/{d.get('total',0)}")

@bot.message_handler(commands=['quizreset'])
def quiz_reset(m):
    _quiz_stats_ref(m.from_user.id).delete()
    bot.send_message(m.chat.id, "Счёт сброшен.")

# Создаем переменную tz для временной зоны
tz = pytz.timezone('Asia/Jerusalem')
# ======= ФРАЗЫ ДНЯ (из файла + одна фраза на день) =======

# 0) РЕЗЕРВ — на случай, если файла нет/сломался
FALLBACK_PHRASES = [
    {"he": "סבבה",              "ru": "окей; норм",                    "note": "разговорное «ок»"},
    {"he": "אין בעיה",          "ru": "без проблем",                   "note": ""},
    {"he": "יאללה, נתקדם",      "ru": "ну поехали, двигаемся",         "note": ""},
    {"he": "בא לי קפה",         "ru": "мне хочется кофе",              "note": "בא לי — «мне хочется»"},
    {"he": "כמה זה יוצא?",      "ru": "сколько выходит?",              "note": "про цену/итог"},
    {"he": "סגרתי פינה",        "ru": "закрыла вопрос; разобралась",   "note": "сленг"},
    {"he": "יאללה, זזתי",       "ru": "ладно, я пошла",                "note": "букв. «двинулась»"},
    {"he": "שניה, אני בודקת",   "ru": "секунду, я проверю",            "note": ""},
]

# 1) Загружаем БД фраз из JSON, если он есть; иначе — резерв
def load_phrase_db():
    path = os.getenv("PHRASES_FILE", "phrases.json")  # можно переопределить переменной окружения
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list) and all(("he" in x and "ru" in x) for x in data)
        return data
    except Exception as e:
        print(f"[phrases] using FALLBACK (reason: {e})")
        return FALLBACK_PHRASES

phrase_db = load_phrase_db()

# 2) Одна и та же фраза на сегодня — для всех (по дате и TZ)
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

# 3) Анти-дубли: не слать одной и той же пользователю дважды за день
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

    recipients = ALLOWED_USERS  # у тебя уже грузится из Firestore
    for user_id in recipients:
        if _get_last_pod_date(user_id) == today:
            continue
        try:
            bot.send_message(user_id, msg, parse_mode="Markdown")
            _set_last_pod_date(user_id, today)
        except Exception as e:
            print(f"[pod] send failed for {user_id}: {e}")

# 4) Планировщик: каждый день в 08:00 по Израилю
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
            _schedule_next_8am()  # перепланировать на завтра

    threading.Timer(delay, _run).start()

# Вызвать ОДИН раз при старте бота:
_schedule_next_8am()

# (опционально, для ручного пинка из чата)
@bot.message_handler(commands=['pod'])
def cmd_pod(m):
    send_phrase_of_the_day_now()
    bot.send_message(m.chat.id, "Фразу дня разослала всем (кто ещё не получал сегодня).")

# 1) Резервные фразы на случай, если phrases.json не загрузится
DEFAULT_PHRASES = [
    {"he": "לאט לאט", "ru": "Постепенно / Не спеши", "note": "Популярная фраза — о терпении, спокойствии."},
    {"he": "יאללה", "ru": "Давай / Ну же!", "note": "Многофункциональный сленг, призыв к действию."},
    {"he": "חבל על הזמן", "ru": "Круто! / Отлично!", "note": "Букв. 'Жаль времени', но в сленге — 'супер'."},
    # можешь оставить тут ещё несколько базовых как запас
]

def load_phrases(path: str) -> list:
    """Загружаем список фраз из JSON; если не вышло — берём резерв."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert isinstance(data, list) and data, "phrases.json пустой"
            return data
    except Exception as e:
        print(f"[phrases] не удалось загрузить {path}: {e}")
        return DEFAULT_PHRASES

# Можно переопределить путь через переменную окружения PHRASES_PATH
PHRASES_PATH = os.getenv("PHRASES_PATH", "phrases.json")
phrase_db = load_phrases(PHRASES_PATH)

def get_today_phrase(dt=None):
    """Возвращает одну и ту же фразу на текущую дату (Asia/Jerusalem)."""
    dt = dt or datetime.now(tz)  # tz у тебя уже задан: tz = pytz.timezone('Asia/Jerusalem')
    day_key = dt.strftime("%Y-%m-%d")
    # детерминированный индекс через хэш даты
    h = hashlib.md5(day_key.encode("utf-8")).hexdigest()
    idx = int(h, 16) % len(phrase_db)
    return phrase_db[idx]

@bot.message_handler(commands=['daily'])
def send_daily_now(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return

    phrase = get_today_phrase()
    msg = (
        f"☀️ בוקר טוב!\nКак дела? Вот тебе фраза дня:\n\n"
        f"🗣 *{phrase['he']}*\n"
        f"📘 Перевод: _{phrase['ru']}_\n"
        f"💬 Пояснение: {phrase['note']}"
    )
    bot.send_message(message.chat.id, msg, parse_mode='Markdown')

# ======= РАССЫЛКА =======
def send_daily_phrase():
    """Отправляем всем одну и ту же фразу за сегодняшний день."""
    phrase = get_today_phrase()
    msg = (
        f"☀️ בוקר טוב!\nКак дела? Вот тебе фраза дня:\n\n"
        f"🗣 *{phrase['he']}*\n"
        f"📘 Перевод: _{phrase['ru']}_\n"
        f"💬 Пояснение: {phrase['note']}"
    )
    for user_id in ALLOWED_USERS:
        try:
            bot.send_message(user_id, msg, parse_mode='Markdown')
        except Exception as e:
            print(f"Ошибка при отправке фразы дня пользователю {user_id}: {e}")

def schedule_daily_phrase():
    """Проверяем время и шлём в 08:00 по Иерусалиму."""
    while True:
        now = datetime.datetime.now(tz)
        if now.hour == 8 and now.minute == 0:
            send_daily_phrase()
            time.sleep(60)  # чтобы не отправить дважды в ту же минуту
        time.sleep(1)

# ======= КНОПКИ =======
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

def check_access(user_id):
    return not ALLOWED_USERS or user_id in ALLOWED_USERS

# ======= ОБРАБОТЧИКИ =======
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

        bot.send_message(
            message.chat.id,
            f"📘 Перевод:\n*{translated_text}*",
            reply_markup=get_keyboard(),
            parse_mode='Markdown'
        )

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
        # 1) Скачиваем файл как есть (.ogg у голосовых в Telegram)
        file_info = bot.get_file(
            message.voice.file_id if message.content_type == 'voice' else message.audio.file_id
        )
        data = bot.download_file(file_info.file_path)

        tmp_path = "voice.ogg"
        with open(tmp_path, "wb") as f:
            f.write(data)

        # 2) Без ffmpeg — сразу отдаём .ogg в Whisper
        with open(tmp_path, "rb") as audio_file:
            try:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="he"
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

    except Exception as e:
        print(f"Ошибка с аудио: {e}")
        bot.send_message(message.chat.id, "Не удалось обработать аудио 😢")
    finally:
        # удаляем временный файл
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
            "грамматическую форму глаголов; объясни сленг/идиомы и происхождение, если есть; "
            "дай короткий пример использования."
        )

        try:
            answer = ask_gpt(
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": text},
                ],
                model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            )
            
            if answer is None:
                # Если OpenAI недоступен, используем офлайн режим
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
            # Фолбэк на офлайн режим
            local = explain_local(text)
            bot.send_message(call.message.chat.id, f"🧠 Объяснение (офлайн):\n{local}")

    elif call.data == "new":
        text = user_translations.get(call.message.chat.id)
        if text:
            try:
                translated_text = translate_text(text)
                bot.send_message(
                    call.message.chat.id,
                    f"📘 Новый перевод:\n*{translated_text}*",
                    reply_markup=get_keyboard(),
                    parse_mode='Markdown'
                )
            except Exception as e:
                print(f"Ошибка повторного перевода: {e}")
                bot.send_message(call.message.chat.id, "Ошибка при переводе 🫣")

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


# ======= GRACEFUL SHUTDOWN =======
def signal_handler(sig, frame):
    print('\n🛑 Получен сигнал завершения. Останавливаю бота...')
    bot.stop_polling()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ======= ЗАПУСК =======
print("🚀 AhlaBot запущен с защитой от дублей ✅")

def schedule_thread():
    schedule.every().day.at("08:00").do(send_daily_phrase)
    while True:
        schedule.run_pending()
        time.sleep(30)

threading.Thread(target=schedule_thread, daemon=True).start()

# 🔒 ЗАЩИЩЕННЫЙ ЗАПУСК
if __name__ == "__main__":
    try:
        print("⏳ Запускаю infinity_polling...")
        bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True, allowed_updates=['message','callback_query'])

    except Exception as e:
        print(f"Критическая ошибка: {e}")
        sys.exit(1)