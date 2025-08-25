# --- добавьте в самый верх main.py ---
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # простой healthcheck на /healthz
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Ahla-bot is running")

def keepalive_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=keepalive_server, daemon=True).start()
# --- дальше ваш код как был (инициализация бота и т.д.) ---

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from deep_translator import GoogleTranslator
import openai
import re
HEB_RE = re.compile(r'[\u0590-\u05FF]')
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
    try:
        # если в тексте есть символы иврита – считаем источник 'he'
        if HEB_RE.search(text):
            return GoogleTranslator(source='he', target='ru').translate(text)
        # иначе как раньше
        return GoogleTranslator(source='auto', target='ru').translate(text)
    except Exception as e:
        print(f"Ошибка перевода: {e}")
        return "⚠️ Ошибка перевода"

    

user_translations = {}

# ======= НАСТРОЙКИ =======
TOKEN = '8147753305:AAEbWrC9D1hWM2xtK5L87XIGkD9GZAYcvFU'
import os
openai.api_key = os.getenv("OPENAI_API_KEY")
bot = telebot.TeleBot(TOKEN)
user_translations = {}
user_data = {}
saved_translations = {}
saved_explanations = {}
saved_audio = {}

# ======= Firebase =======
import os
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

def schedule_daily_phrase():
    while True:
        now = datetime.datetime.now(tz)
        if now.hour == 8 and now.minute == 0:
            send_daily_phrase()
            time.sleep(60)
        time.sleep(1)

# Запустить в фоновом потоке
threading.Thread(target=schedule_daily_phrase, daemon=True).start()


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
    {"he": "תן בראש", "ru": "Вперёд! / Покажи класс!", "note": "Поддержка, мотивация."},
    {"he": "מה העניינים?", "ru": "Как дела?", "note": "Разговорный, дружелюбный вариант."},
    {"he": "יאללה נשתמע", "ru": "Услышимся", "note": "Форма прощания."},
    {"he": "מה אתה דפוק?", "ru": "Ты с ума сошёл?", "note": "Грубовато, шутливо. Неформально."},
    {"he": "יאללה חגיגה", "ru": "Погнали веселиться!", "note": "Сленг, зов к веселью."},
    {"he": "יש מצב", "ru": "Возможно", "note": "Сленговая форма 'может быть'."},
    {"he": "נו באמת", "ru": "Да ладно тебе!", "note": "Неверие, раздражение."},
    {"he": "מה הלחץ?", "ru": "Чего ты паникуешь?", "note": "Фраза для успокоения."},
    {"he": "תעשה חיים", "ru": "Хорошего отдыха!", "note": "Пожелание веселья, отдыха."},
    {"he": "נראה מה יהיה", "ru": "Поживём — увидим", "note": "Философский настрой."},
    {"he": "אין לי כוח", "ru": "Нет сил / Не могу", "note": "Сленг, уставшее состояние."},
    {"he": "בא לי", "ru": "Мне хочется", "note": "Фраза желания."},
    {"he": "לא בא לי", "ru": "Мне не хочется", "note": "Противоположность 'בא לי'."},
    {"he": "טוב נו", "ru": "Ну ладно", "note": "Согласие с лёгким недовольством."},
    {"he": "שיהיה", "ru": "Пусть будет", "note": "Покорность, согласие."},
    {"he": "יאללה בלגן", "ru": "Погнали в хаос!", "note": "Весёлый сленг, перед вечеринкой или движом."},
    {"he": "שקט!", "ru": "Тихо!", "note": "Императив, может быть резко."},
    {"he": "נשמע טוב", "ru": "Звучит хорошо", "note": "Принятие идеи, согласие."},
    {"he": "לך על זה", "ru": "Действуй!", "note": "Мотивация, поддержка."},
    {"he": "חייב לחשוב על זה", "ru": "Надо подумать", "note": "Вежливый отказ или сомнение."},
    {"he": "קח את הזמן", "ru": "Не торопись", "note": "Фраза поддержки, без давления."}
]


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



# ======= Без выдумки — перевод =======
def safe_translate(text, chat_id):
    key = (chat_id, text)
    if key in saved_translations:
        return saved_translations[key]
    try:
        translated = translate_text(text)
        saved_translations[key] = translated
        return translated
    except Exception:
        return "🤷‍♂️ סליחה, я не знаю, как правильно перевести эту фразу."

# ======= Без выдумки — объяснение =======
def safe_explanation(text, chat_id):
    key = (chat_id, text)
    if key in saved_explanations:
        return saved_explanations[key]
    try:
        response = openai.ChatCompletion.create(
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
        saved_explanations[key] = answer
        return answer
    except Exception:
        return "🤷‍♂️ סליחה, я не знаю, как объяснить эту фразу."


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

# ======= ТЕКСТ =======
@bot.message_handler(content_types=['text'])
def handle_text(message):
    if message.forward_from or message.forward_from_chat:
        bot.send_message(message.chat.id, "📩 Пересланное сообщение. Хотите перевести?", reply_markup=get_yes_no_keyboard())
        bot.register_next_step_handler(message, save_forwarded_text)
    else:
        try:
            orig = message.text.strip()
            user_translations[message.chat.id] = orig           # <-- сохраняем ОРИГИНАЛ
            translated_text = translate_text(orig)

            bot.send_message(
                message.chat.id,
                f"📘 Перевод:\n*{translated_text}*",
                reply_markup=get_keyboard(),
                parse_mode='Markdown'
                )

        except Exception as e:
            bot.send_message(message.chat.id, "Ошибка при переводе 🫣")

# ======= ГОЛОС =======
@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message):
    if message.forward_from or message.forward_from_chat:
        bot.send_message(message.chat.id, "📩 Пересланное аудио. Хотите расшифровать и перевести?", reply_markup=get_yes_no_keyboard())
        message.content_type = 'audio'
        bot.register_next_step_handler(message, save_forwarded_audio)
    else:
        process_audio(message)

def process_audio(message):
    try:
        file_info = bot.get_file(message.voice.file_id if message.content_type == 'voice' else message.audio.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        with open("voice.ogg", 'wb') as f:
            f.write(downloaded_file)

        os.system("ffmpeg -y -i voice.ogg voice.mp3")

        audio_file = open("voice.mp3", "rb")
        try:
            transcript = openai.Audio.transcribe("whisper-1", audio_file, language="he")
        except openai.error.APIError as api_err:
            if "overloaded" in str(api_err).lower():
                bot.send_message(message.chat.id, "🤖 Сейчас сервер перегружен, попробуй чуть позже.")
            else:
                bot.send_message(message.chat.id, "⚠️ Ошибка при расшифровке аудио.")
            return
        finally:
            audio_file.close()

        hebrew_text = transcript['text']
        translated_text = translate_text(hebrew_text)
        user_translations[message.chat.id] = hebrew_text

        bot.send_message(
            message.chat.id,
            f"🗣 Распознанный текст:\n_{hebrew_text}_\n\n📘 Перевод:\n*{translated_text}*",
            parse_mode='Markdown',
            reply_markup=get_keyboard()
        )

    except Exception as e:
        print("Ошибка с аудио:", e)
        bot.send_message(message.chat.id, "Не удалось обработать аудио 😢")
    finally:
        if os.path.exists("voice.ogg"):
            os.remove("voice.ogg")
        if os.path.exists("voice.mp3"):
            os.remove("voice.mp3")


# ======= КНОПКИ CALLBACK =======
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global user_data
    if call.data == "explain":
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        text = user_translations.get(call.message.chat.id)
        if not text:
            bot.send_message(call.message.chat.id, "Нет текста для объяснения.")
            return

        try:
            response = openai.ChatCompletion.create(
                model="gpt-4o",  # 👈 Это GPT‑4.1
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
            print("GPT error:", e)
            bot.send_message(call.message.chat.id, "⚠️ Не удалось получить объяснение.")




def save_forwarded_audio(message):
    process_audio(message)

def save_forwarded_text(message):
    handle_text(message)

# ======= СТАРТ =======
print("AhlaBot запущен ✅")
def schedule_thread():
    schedule.every().day.at("08:00").do(send_daily_phrase)
    while True:
        schedule.run_pending()
        time.sleep(30)

threading.Thread(target=schedule_thread, daemon=True).start()
bot.infinity_polling()
