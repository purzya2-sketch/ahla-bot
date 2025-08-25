# --- добавьте в самый верх main.py ---
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from openai import OpenAI
client = OpenAI()  # возьмёт OPENAI_API_KEY из окружения
import time
import sys
import signal

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

# --- ИМПОРТЫ ---
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from deep_translator import GoogleTranslator, MyMemoryTranslator
import re
HEB_RE = re.compile(r'[\u0590-\u05FF]')
import openai
import requests
import datetime
import random
import firebase_admin
from firebase_admin import credentials, firestore
import schedule
import pytz

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
        time.sleep(10)
        
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

def translate_text(text):
    src = 'he' if HEB_RE.search(text) else 'auto'
    
    last_err = None
    for _ in range(2):
        try:
            return GoogleTranslator(source=src, target='ru').translate(text)
        except Exception as e:
            last_err = e
            time.sleep(0.4)

    try:
        return MyMemoryTranslator(source=src, target='ru').translate(text)
    except Exception as e2:
        print(f"Ошибка перевода: Google: {last_err} | MyMemory: {e2}")
        return "⚠️ Ошибка перевода"

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
]

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

# ======= РАССЫЛКА =======
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

        try:
            client 
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
        bot.infinity_polling(timeout=20, long_polling_timeout=20)
        bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)

    except Exception as e:
        print(f"Критическая ошибка: {e}")
        sys.exit(1)