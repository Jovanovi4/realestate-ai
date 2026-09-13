import requests


class AIService:

    URL = "http://127.0.0.1:11434/api/generate"

    MODEL = "qwen2.5:3b"

    @classmethod
    def generate_description(cls, property):

        prompt = f"""
Ты профессиональный маркетолог недвижимости.

Создай продающее описание.

Название:
{property.title}

Тип:
{property.get_property_type_display()}

Цена:
{property.price}

Адрес:
{property.address}

Площадь:
{property.area}

Комнаты:
{property.rooms}

Пиши красиво, профессионально и без выдуманных фактов.
"""

        try:

            response = requests.post(

                cls.URL,

                json={
                    "model": cls.MODEL,
                    "prompt": prompt,
                    "stream": False,
                },

                timeout=120,

            )

            response.raise_for_status()

            return response.json()["response"]

        except requests.exceptions.ConnectionError:

            return (
                "⚠️ Ollama не запущена.\n\n"
                "Запусти команду:\n\n"
                "ollama serve"
            )

        except Exception as e:

            return f"Ошибка AI: {e}"