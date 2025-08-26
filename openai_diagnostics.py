#!/usr/bin/env python3
"""
Скрипт для диагностики проблем с OpenAI API
Запустите этот файл отдельно: python openai_diagnostics.py
"""

import os
import sys
import requests
from openai import OpenAI
import json
from datetime import datetime

def print_header(title):
    print(f"\n{'='*50}")
    print(f"🔍 {title}")
    print(f"{'='*50}")

def check_environment():
    """Проверяем переменные окружения"""
    print_header("ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ")
    
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        print(f"✅ OPENAI_API_KEY найден")
        print(f"📝 Начинается с: {api_key[:10]}...")
        print(f"📏 Длина ключа: {len(api_key)} символов")
        
        # Проверяем формат ключа
        if api_key.startswith("sk-"):
            print("✅ Формат ключа корректный (начинается с sk-)")
        else:
            print("⚠️ Возможно неправильный формат ключа (должен начинаться с sk-)")
    else:
        print("❌ OPENAI_API_KEY не найден!")
        print("💡 Установите переменную окружения:")
        print("   export OPENAI_API_KEY='your-api-key-here'")
        return False
    
    # Проверяем другие переменные
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    print(f"📋 Модель: {model}")
    
    return True

def check_internet_connection():
    """Проверяем интернет соединение"""
    print_header("ПРОВЕРКА ИНТЕРНЕТ СОЕДИНЕНИЯ")
    
    test_urls = [
        "https://www.google.com",
        "https://api.openai.com",
        "https://openai.com"
    ]
    
    for url in test_urls:
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                print(f"✅ {url} - доступен")
            else:
                print(f"⚠️ {url} - код ответа: {response.status_code}")
        except requests.exceptions.Timeout:
            print(f"❌ {url} - таймаут")
        except requests.exceptions.ConnectionError:
            print(f"❌ {url} - ошибка подключения")
        except Exception as e:
            print(f"❌ {url} - ошибка: {e}")

def check_openai_status():
    """Проверяем статус сервисов OpenAI"""
    print_header("ПРОВЕРКА СТАТУСА OPENAI")
    
    try:
        # Проверяем страницу статуса OpenAI
        response = requests.get("https://status.openai.com/api/v2/status.json", timeout=10)
        if response.status_code == 200:
            data = response.json()
            status = data.get('status', {}).get('description', 'Unknown')
            print(f"🌐 Статус OpenAI: {status}")
        else:
            print(f"⚠️ Не удалось получить статус OpenAI")
    except Exception as e:
        print(f"❌ Ошибка при проверке статуса: {e}")

def test_openai_api():
    """Тестируем API OpenAI"""
    print_header("ТЕСТИРОВАНИЕ OPENAI API")
    
    try:
        client = OpenAI()  # Использует переменную окружения
        
        # Проверяем список моделей
        print("🔍 Получаем список доступных моделей...")
        try:
            models = client.models.list()
            available_models = [model.id for model in models.data]
            print(f"✅ Доступно моделей: {len(available_models)}")
            
            # Проверяем нужные модели
            needed_models = ["gpt-4o", "gpt-4", "gpt-3.5-turbo", "whisper-1"]
            for model in needed_models:
                if model in available_models:
                    print(f"✅ Модель {model} доступна")
                else:
                    print(f"❌ Модель {model} недоступна")
                    
        except Exception as e:
            print(f"❌ Ошибка при получении списка моделей: {e}")
            return False
        
        # Тестовый запрос к чат-модели
        print("\n🔍 Тестируем chat completions...")
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",  # Используем более дешевую модель для теста
                messages=[{"role": "user", "content": "Привет! Это тест."}],
                max_tokens=10,
                temperature=0
            )
            print(f"✅ Chat API работает!")
            print(f"📝 Ответ: {response.choices[0].message.content}")
            
        except Exception as e:
            print(f"❌ Ошибка Chat API: {e}")
            
            # Анализируем тип ошибки
            error_str = str(e).lower()
            if "authentication" in error_str:
                print("🔑 Проблема с аутентификацией - проверьте API ключ")
            elif "quota" in error_str or "billing" in error_str:
                print("💳 Проблема с квотой или биллингом")
            elif "rate" in error_str:
                print("🚦 Превышен лимит запросов")
            elif "connection" in error_str or "timeout" in error_str:
                print("🌐 Проблема с сетевым соединением")
            return False
            
    except Exception as e:
        print(f"❌ Критическая ошибка OpenAI: {e}")
        return False
    
    return True

def check_account_info():
    """Проверяем информацию об аккаунте"""
    print_header("ИНФОРМАЦИЯ ОБ АККАУНТЕ")
    
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("❌ API ключ не найден")
        return
    
    try:
        # Делаем прямой HTTP запрос для проверки биллинга
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        # Проверяем биллинг (может не работать для всех типов ключей)
        try:
            response = requests.get(
                "https://api.openai.com/v1/usage?date=2024-01-01", 
                headers=headers, 
                timeout=10
            )
            if response.status_code == 200:
                print("✅ API ключ валиден и имеет доступ к биллинговой информации")
            elif response.status_code == 401:
                print("❌ API ключ недействителен")
            elif response.status_code == 403:
                print("⚠️ API ключ валиден, но нет доступа к биллингу")
            else:
                print(f"⚠️ Неожиданный ответ: {response.status_code}")
        except Exception as e:
            print(f"⚠️ Не удалось проверить биллинг: {e}")
            
    except Exception as e:
        print(f"❌ Ошибка проверки аккаунта: {e}")

def check_regional_access():
    """Проверяем региональные ограничения"""
    print_header("ПРОВЕРКА РЕГИОНАЛЬНЫХ ОГРАНИЧЕНИЙ")
    
    try:
        # Получаем информацию о своем IP
        response = requests.get("https://ipapi.co/json/", timeout=10)
        if response.status_code == 200:
            data = response.json()
            country = data.get('country_name', 'Unknown')
            city = data.get('city', 'Unknown')
            print(f"🌍 Ваше местоположение: {city}, {country}")
            
            # Проверяем, есть ли ограничения для этой страны
            restricted_countries = ['Russia', 'China', 'Iran', 'North Korea']
            if country in restricted_countries:
                print(f"⚠️ OpenAI может быть ограничен в {country}")
                print("💡 Попробуйте использовать VPN")
            else:
                print(f"✅ OpenAI должен работать в {country}")
        else:
            print("⚠️ Не удалось определить местоположение")
            
    except Exception as e:
        print(f"❌ Ошибка проверки региона: {e}")

def main():
    print("🤖 Диагностика OpenAI API")
    print(f"⏰ Время запуска: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Последовательно проверяем все аспекты
    steps = [
        check_environment,
        check_internet_connection,
        check_openai_status,
        check_regional_access,
        check_account_info,
        test_openai_api,
    ]
    
    for step in steps:
        try:
            result = step()
            if result is False:
                print(f"\n❌ Критическая ошибка в {step.__name__}")
        except Exception as e:
            print(f"\n❌ Неожиданная ошибка в {step.__name__}: {e}")
    
    print_header("РЕКОМЕНДАЦИИ")
    print("1. ✅ Проверьте, что OPENAI_API_KEY правильно установлен")
    print("2. 💳 Убедитесь, что на аккаунте OpenAI есть активная подписка/кредиты")
    print("3. 🌐 Проверьте интернет-соединение")
    print("4. 🚦 Если превышены лимиты - подождите или увеличьте квоту")
    print("5. 🔄 Попробуйте перезапустить приложение")
    print("6. 🌍 При региональных ограничениях используйте VPN")
    
    print(f"\n🏁 Диагностика завершена: {datetime.now().strftime('%H:%M:%S')}")

if __name__ == "__main__":
    main()