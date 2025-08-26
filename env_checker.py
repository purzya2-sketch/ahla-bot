#!/usr/bin/env python3
"""
Быстрая проверка переменных окружения
"""
import os

print("🔍 Проверка переменных окружения:")
print("-" * 40)

# Основные переменные для бота
variables = {
    'OPENAI_API_KEY': 'OpenAI API ключ',
    'OPENAI_MODEL': 'Модель OpenAI (опционально)',
    'FIREBASE_CREDENTIALS_PATH': 'Путь к Firebase ключу (опционально)',
    'PORT': 'Порт для health сервера',
    'PHRASES_PATH': 'Путь к файлу с фразами (опционально)'
}

for var_name, description in variables.items():
    value = os.getenv(var_name)
    if value:
        if 'KEY' in var_name:
            # Скрываем ключи частично
            display_value = f"{value[:10]}..." if len(value) > 10 else "***"
        else:
            display_value = value
        print(f"✅ {var_name}: {display_value}")
    else:
        print(f"❌ {var_name}: не установлена")

print("\n📋 Как установить переменные окружения:")
print("Windows:")
print("  set OPENAI_API_KEY=your-key-here")
print("  python main.py")
print("\nLinux/Mac:")
print("  export OPENAI_API_KEY='your-key-here'")
print("  python main.py")
print("\nИли создайте .env файл:")
print("  OPENAI_API_KEY=your-key-here")
print("  OPENAI_MODEL=gpt-4o")