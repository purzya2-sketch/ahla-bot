# --- добавьте в самый верх main.py ---
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthHandler(BaseHTTPRequestHandler):
    def _ok_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        # Можно отвечать "ok" на / и /health
        self._ok_headers()
        try:
            self.wfile.write(b"ok")
        except BrokenPipeError:
            pass

    def do_HEAD(self):
        # HEAD должен возвращать те же заголовки 200, но без тела
        self._ok_headers()

def run_health_server():
    port = int(os.environ.get("PORT", "10000"))  # Render задаёт PORT
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server on port {port}")
    server.serve_forever()

# Запускаем в отдельном потоке, чтобы не мешать боту
threading.Thread(target=run_health_server, daemon=True).start()

# --- дальше ваш код как был (инициализация бота и т.д.) ---

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from deep_translator import GoogleTranslator, MyMemoryTranslator
import re
HEB_RE = re.compile(r'[\u0590-\u05FF]')
import openai
import requests
import os
import datetime
import random
import firebase_admin
from firebase_admin import credentials, firestore
import schedule
import time
import threading
import pytz

def translate_text(text):
    src = 'he' if HEB_RE.search(text) else 'auto'

    # 1) Пробуем Google 2 раза (иногда кратковременный сбой)
    last_err = None
    for _ in range(2):
        try:
            return GoogleTranslator(source=src, target='ru').translate(text)
        except Exception as e:
            last_err = e
            time.sleep(0.4)  # микропаузa и повтор

    # 2) Фолбэк: MyMemory (чуть медленнее/ограничения, но стабильно)
    try:
        return MyMemoryTranslator(source=src, target='ru').translate(text)
    except Exception as e2:
        print(f"Ошибка перевода: Google: {last_err} | MyMemory: {e2}")
        return "⚠️ Ошибка перевода"

# ======= НАСТРОЙКИ =======
TOKEN = '8147753305:AAEbWrC9D1hWM2xtK5L87XIGkD9GZAYcvFU'
openai.api_key = os.getenv("OPENAI_API_KEY")
bot = telebot.TeleBot(TOKEN)

# Глобальные словари
user_translations = {}
user_data = {}
saved_translations = {}
saved_explanations = {}
saved_audio = {}

# ======= Firebase =======
from dotenv import load_dotenv
load_dotenv()

def _find_firebase_key():
    """Возвращает путь к ключу Firebase, пробуя несколько вариантов."""
    candidates = []

    # 1) Путь из переменной окружения (удобно для локальной разработки)
    env_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
    if env_path:
        candidates.append(env_path)

    # 2) Файл рядом с проектом (если есть локальный json в репозитории)
    repo_file = os.path.join(
        os.path.dirname(__file__),
        "trivia-game-79e1b-firebase-adminsdk-fbsvc-20be34c499.json"
    )
    candidates.append(repo_file)

    # 3) Путь Secret Files на Render
    candidates.append("/etc/secrets/firebase-key.json")

    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Не найден ключ Firebase. Укажите FIREBASE_CREDENTIALS_PATH, "
        "или положите JSON рядом с проектом, или настройте Secret Files на Render."
    )

firebase_key_path = _find_firebase_key()
cred = credentials.Certificate(firebase_key_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ======= Получить список разрешённых пользователей из Firebase =======
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

# ✅ Команда /id
@bot.message_handler(commands=['id'])
def send_user_id(message):
    bot.send_message(message.chat.id, f"👤 Твой Telegram ID: `{message.from_user.id}`", parse_mode='Markdown')

# ======= ФРАЗЫ ДНЯ =======
phrase_db = [
    {"he": "לאט לאט", "ru": "Постепенно / Не спеши", "note": "Популярная фраза — о терпении, спокойствии."},
    {"he": "יאללה", "ru": "Давай / Ну же!", "note": "Многофункциональный сленг, призыв к действию."},
    {"he": "חבל על הזמן", "ru": "Круто! / Отлично!", "note": "Букв. 'Жаль времени', но в сленге — 'супер'."},
    {"he": "נראה לי", "ru": "Мне кажется", "note": "Фраза мнения, часто используется в разговоре."},
    {"he": "מה פתאום!", "ru": "С чего вдруг?!", "note": "Удивление или несогласие, очень разговорно."},
    {"he": "כפרה עליך", "ru": "Душа моя / Спасибо", "note": "Сленг, тёплое обращение или благодарность."},
    {"he": "בלי לחץ", "ru": "Без стресса / Не спеши", "note": "Успокаивающая фраза, антипаника."},
    {"he": "יאללה נלך", "ru": "Ну, пойдём", "note": "Пора идти — по-дружески, с призывом."},
    {"he": "אין עליך", "ru": "Ты лучший!", "note": "Сленговая похвала, прямой комплимент."},
    {"he": "סמוך עליי", "ru": "Положись на меня", "note": "Фраза уверенности, поддержка."},
    {"he": "מה נסגר איתך?", "ru": "Что с тобой?", "note": "Раздражённый или шутливый тон, сленг."},
    {"he": "סגור", "ru": "Договорились", "note": "Сленг. Используется при согласии."},
    {"he": "חייב לזוז", "ru": "Мне пора", "note": "Разговорный способ попрощаться."},
    {"he": "קטע", "ru": "Прикол / Ситуация", "note": "Может означать момент, ситуация, прикол."},
    {"he": "תשמור על עצמך", "ru": "Береги себя", "note": "Прощальная заботливая фраза."},
    {"he": "אל תדאג", "ru": "Не волнуйся", "note": "Успокаивающая фраза."},
    {"he": "בקטנה", "ru": "Пустяки", "note": "Фраза при ответе на благодарность или просьбу."},
    {"he": "מה אתה אומר", "ru": "Да ты что!", "note": "Удивление или интерес."},
    {"he": "סתם", "ru": "Просто так / Шутка", "note": "Сленг. Для разрядки."},
    {"he": "תן בראש", "ru": "Вперёд! / Покажи класс!", "note": "Поддержка, мотивация."}
]

# Команда /daily — отправить фразу дня вручную
@bot.message_handler(commands=['daily'])
def send_daily_now(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return

    phrase = random.choice(phrase_db)
    msg = (
        f"☀️ בוקר טוב!\nКак дела? Вот тебе фраза дня:\n\n"
        f"🗣 *{phrase['he']}*\n"
        f"📘 Перевод: _{phrase['ru']}_\n"
        f"💬 Пояснение: {phrase['note']}"
    )
    bot.send_message(message.chat.id, msg, parse_mode='Markdown')

# ======= РАССЫЛКА ФРАЗЫ ДНЯ В 8:00 ПО ИЗРАИЛЮ =======
tz = pytz.timezone('Asia/Jerusalem')

def send_daily_phrase():
    for user_id in ALLOWED_USERS:
        try:
            phrase = random.choice(phrase_db)
            msg = (
                f"☀️ בוקר טוב!\nКак дела? Вот тебе фраза дня:\n\n"
                f"🗣 *{phrase['he']}*\n"
                f"📘 Перевод: _{phrase['ru']}_\n"
                f"💬 Пояснение: {phrase['note']}"
            )
            bot.send_message(user_id, msg, parse_mode='Markdown')
        except Exception as e:
            print(f"Ошибка при отправке фразы дня пользователю {user_id}: {e}")

def schedule_daily_phrase():
    while True:
        now = datetime.datetime.now(tz)
        if now.hour == 8 and now.minute == 0:
            send_daily_phrase()
            time.sleep(60)
        time.sleep(1)

# Запустить в фоновом потоке
threading.Thread(target=schedule_daily_phrase, daemon=True).start()

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

# ======= ПРОВЕРКА ДОСТУПА =======
def check_access(user_id):
    return not ALLOWED_USERS or user_id in ALLOWED_USERS

# ======= ТЕКСТ =======
@bot.message_handler(content_types=['text'])
def handle_text(message):
    # Проверка доступа
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return
    
    # Обработка пересланных сообщений
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

# ======= ГОЛОС =======
@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message):
    # Проверка доступа
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
        downloaded_file = bot.download_file(file_info.file_path)

        with open("voice.ogg", 'wb') as f:
            f.write(downloaded_file)

        os.system("ffmpeg -y -i voice.ogg voice.mp3")

        with open("voice.mp3", "rb") as audio_file:
            try:
                # Обновленный синтаксис для новой версии OpenAI API
                client = openai.OpenAI()
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
        # Очистка временных файлов
        for file in ["voice.ogg", "voice.mp3"]:
            if os.path.exists(file):
                os.remove(file)

# ======= КНОПКИ CALLBACK =======
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    bot.answer_callback_query(call.id)
    
    if call.data == "explain":
        text = user_translations.get(call.message.chat.id)
        if not text:
            bot.send_message(call.message.chat.id, "Нет текста для объяснения.")
            return

        try:
            client = openai.OpenAI()
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": (
                        "Ты — опытный преподаватель разговорного иврита. "
                        "Проанализируй фразу на иврите: выдели перевод, корень, биньян, грамматическую форму каждого глагола. "
                        "Обязательно объясни сленг, разговорные выражения и происхождение, если есть. "
                        "Приведи пример использования в другой фразе, если это уместно."
                    )},
                    {"role": "user", "content": text}
                ],
                temperature=0.4
            )

            answer = response.choices[0].message.content.strip()
            bot.send_message(call.message.chat.id, f"🧠 Объяснение:\n{answer}")

        except Exception as e:
            print(f"GPT error: {e}")
            bot.send_message(call.message.chat.id, "⚠️ Не удалось получить объяснение.")
    
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
        
        # Очистка данных
        if call.message.chat.id in user_data:
            del user_data[call.message.chat.id]
    
    elif call.data == "cancel":
        bot.send_message(call.message.chat.id, "❌ Отменено")
        if call.message.chat.id in user_data:
            del user_data[call.message.chat.id]

# ======= СТАРТ =======
print("AhlaBot запущен ✅")

def schedule_thread():
    schedule.every().day.at("08:00").do(send_daily_phrase)
    while True:
        schedule.run_pending()
        time.sleep(30)

threading.Thread(target=schedule_thread, daemon=True).start()

if __name__ == "__main__":
    bot.infinity_polling()