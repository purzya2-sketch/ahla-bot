# АЛЬТЕРНАТИВНАЯ ВЕРСИЯ С WEBHOOK (если polling не работает)
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time
import sys
import signal

# --- WEBHOOK HANDLER ---
class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/webhook':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
            
            try:
                update = json.loads(post_data.decode('utf-8'))
                threading.Thread(target=process_webhook_update, args=(update,), daemon=True).start()
            except Exception as e:
                print(f"Webhook error: {e}")
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_GET(self):
        # Health check
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running')

def run_webhook_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"Webhook server running on port {port}")
    server.serve_forever()

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

# --- НАСТРОЙКИ ---
TOKEN = '8147753305:AAEbWrC9D1hWM2xtK5L87XIGkD9GZAYcvFU'
openai.api_key = os.getenv("OPENAI_API_KEY")

# Создаем бота БЕЗ polling
bot = telebot.TeleBot(TOKEN)

# Глобальные словари
user_translations = {}
user_data = {}

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

# --- Firebase ---
from dotenv import load_dotenv
load_dotenv()

def _find_firebase_key():
    candidates = [
        os.getenv("FIREBASE_CREDENTIALS_PATH"),
        os.path.join(os.path.dirname(__file__), "trivia-game-79e1b-firebase-adminsdk-fbsvc-20be34c499.json"),
        "/etc/secrets/firebase-key.json"
    ]
    
    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError("Не найден ключ Firebase")

firebase_key_path = _find_firebase_key()
cred = credentials.Certificate(firebase_key_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Пользователи ---
ALLOWED_USERS = set()
def load_allowed_users():
    try:
        users_ref = db.collection("allowed_users").stream()
        for doc in users_ref:
            ALLOWED_USERS.add(int(doc.id))
        print(f"✅ Загружено {len(ALLOWED_USERS)} пользователей")
    except Exception as e:
        print(f"Ошибка загрузки пользователей: {e}")

load_allowed_users()

# --- ФРАЗЫ ДНЯ ---
phrase_db = [
    {"he": "לאט לאט", "ru": "Постепенно / Не спеши", "note": "Популярная фраза — о терпении, спокойствии."},
    {"he": "יאללה", "ru": "Давай / Ну же!", "note": "Многофункциональный сленг, призыв к действию."},
    {"he": "חבל על הזמן", "ru": "Круто! / Отлично!", "note": "Букв. 'Жаль времени', но в сленге — 'супер'."},
    {"he": "נראה לי", "ru": "Мне кажется", "note": "Фраза мнения, часто используется в разговоре."},
    {"he": "מה פתאום!", "ru": "С чего вдруг?!", "note": "Удивление или несогласие, очень разговорно."}
]

# --- WEBHOOK UPDATE PROCESSOR ---
def process_webhook_update(update):
    try:
        # Преобразуем update в объект telebot
        if 'message' in update:
            message = telebot.types.Message.de_json(update['message'])
            if message.content_type == 'text':
                handle_text(message)
            elif message.content_type in ['voice', 'audio']:
                handle_voice(message)
        elif 'callback_query' in update:
            callback = telebot.types.CallbackQuery.de_json(update['callback_query'])
            handle_callback(callback)
    except Exception as e:
        print(f"Error processing update: {e}")

# --- КНОПКИ ---
def get_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🧠 Объяснить", callback_data="explain"),
        InlineKeyboardButton("🔁 Новый перевод", callback_data="new")
    )
    return markup

def check_access(user_id):
    return not ALLOWED_USERS or user_id in ALLOWED_USERS

# --- HANDLERS ---
def handle_text(message):
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
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

def handle_voice(message):
    if not check_access(message.from_user.id):
        bot.send_message(message.chat.id, "Извини, доступ ограничен 👮‍♀️")
        return
    
    # Voice processing logic here (same as before)
    bot.send_message(message.chat.id, "🎵 Обработка аудио временно недоступна в webhook режиме")

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

# --- SETUP WEBHOOK ---
def setup_webhook():
    try:
        # Get the Render URL
        render_url = os.environ.get('RENDER_EXTERNAL_URL')
        if not render_url:
            print("❌ RENDER_EXTERNAL_URL не найден")
            return False
        
        webhook_url = f"{render_url}/webhook"
        
        # Set webhook
        result = bot.set_webhook(webhook_url)
        if result:
            print(f"✅ Webhook установлен: {webhook_url}")
            return True
        else:
            print("❌ Не удалось установить webhook")
            return False
            
    except Exception as e:
        print(f"Ошибка установки webhook: {e}")
        return False

# --- РАССЫЛКА ---
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
            print(f"Ошибка отправки фразы дня: {e}")

def schedule_daily_phrase():
    while True:
        now = datetime.datetime.now(tz)
        if now.hour == 8 and now.minute == 0:
            send_daily_phrase()
            time.sleep(60)
        time.sleep(1)

# --- ЗАПУСК ---
if __name__ == "__main__":
    print("🚀 Запуск бота с webhook...")
    
    # Устанавливаем webhook
    if setup_webhook():
        # Запускаем планировщик
        threading.Thread(target=schedule_daily_phrase, daemon=True).start()
        
        # Запускаем webhook сервер
        print("✅ Webhook bot запущен")
        run_webhook_server()
    else:
        print("❌ Не удалось настроить webhook")
        sys.exit(1)