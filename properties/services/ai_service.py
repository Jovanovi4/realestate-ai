import re

import requests


class AIService:

    URL = "http://127.0.0.1:11434/api/generate"

    MODEL = "qwen2.5:3b"

    @staticmethod
    def clean_description(text):
        """Convert occasional Markdown formatting from the local model to plain text."""
        text = text.replace("\\*", "*").replace("\\_", "_")
        text = text.replace("**", "").replace("*", "")
        text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

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

Требования к результату:
- Верни только готовое продающее описание, а не повтор карточки объекта.
- Не повторяй отдельно название, тип, цену, адрес, площадь и комнаты.
- Не используй Markdown: никаких звёздочек, решёток, символов **, * или заголовков.
- Пиши обычным чистым текстом в 2–3 коротких абзацах.
- Не выдумывай факты, характеристики или преимущества, которых нет в данных.
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

            return cls.clean_description(response.json()["response"])

        except requests.exceptions.ConnectionError:

            return (
                "⚠️ Ollama не запущена.\n\n"
                "Запусти команду:\n\n"
                "ollama serve"
            )

        except Exception as e:

            return f"Ошибка AI: {e}"
