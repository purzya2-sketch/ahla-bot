from openai import OpenAI, APIConnectionError
import os

def main():
    try:
        client = OpenAI(timeout=10)
        models = client.models.list()
        print("✅ Доступ есть! Моделей:", len(models.data))
        for m in models.data[:5]:
            print("-", m.id)
    except APIConnectionError as e:
        print("❌ NO CONNECT:", e)
    except Exception as e:
        print("⚠️ OTHER:", type(e).__name__, e)

if __name__ == "__main__":
    main()
