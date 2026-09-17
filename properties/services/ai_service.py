import re

import requests
from django.conf import settings


class AIServiceError(Exception):
    pass


class AIService:
    """Provider-neutral generation service for marketing copy based on card data."""

    SYSTEM_PROMPT = """Ты ИИ-помощник риелтора. Пиши только на русском языке.
Используй исключительно факты из блока «Данные объекта». Никогда не добавляй,
не предполагай и не приукрашивай характеристики, инфраструктуру, район, вид,
транспортную доступность, юридический статус, ремонт или любые другие сведения.
Если факта нет в данных, не упоминай его. Не используй Markdown, звёздочки,
решётки, списки или служебные комментарии. Верни только готовый текст."""

    TONE_INSTRUCTIONS = {
        "business": "Тон: деловой, ясный и профессиональный.",
        "premium": "Тон: сдержанно-премиальный, без неподтверждённых превосходных степеней.",
        "concise": "Тон: лаконичный, без воды.",
        "emotional": "Тон: тёплый и эмоциональный, но без выдумывания фактов.",
    }

    CONTENT_INSTRUCTIONS = {
        "headline": "Создай один заголовок объявления длиной до 90 символов. Не добавляй кавычки.",
        "short_description": "Создай короткое описание: один абзац, 250–450 символов.",
        "full_description": "Создай полное продающее описание: 2–3 коротких абзаца, до 1 500 символов.",
    }

    @staticmethod
    def clean_description(text):
        """Convert occasional Markdown formatting from a model to plain text."""
        text = text.replace("\\*", "*").replace("\\_", "_")
        text = text.replace("**", "").replace("*", "")
        text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _property_facts(cls, property):
        facts = [
            ("Название объекта", property.title),
            ("Тип", property.get_property_type_display()),
            ("Статус", property.get_status_display()),
            ("Цена", f"{property.price} {property.get_currency_display()}" if property.price else ""),
            ("Адрес", property.address),
            ("Площадь", f"{property.area} м²" if property.area else ""),
            ("Комнаты", property.rooms),
            ("Санузлы", property.bathrooms),
            ("Этаж", f"{property.floor} из {property.floors_total}" if property.floor and property.floors_total else property.floor),
            ("Площадь участка", f"{property.land_area} м²" if property.land_area else ""),
            ("Год постройки", property.year_built),
            ("Состояние", property.get_condition_display() if property.condition else ""),
            ("Особенности и удобства", property.amenities),
        ]
        return "\n".join(f"- {label}: {value}" for label, value in facts if value not in (None, ""))

    @classmethod
    def build_prompt(cls, property, content_type, tone):
        if content_type not in cls.CONTENT_INSTRUCTIONS or tone not in cls.TONE_INSTRUCTIONS:
            raise AIServiceError("Неизвестный тип текста или тон генерации.")
        return "\n\n".join((
            cls.CONTENT_INSTRUCTIONS[content_type],
            cls.TONE_INSTRUCTIONS[tone],
            "Данные объекта:\n" + cls._property_facts(property),
        ))

    @classmethod
    def generate_content(cls, property, content_type, tone):
        prompt = cls.build_prompt(property, content_type, tone)
        provider = getattr(settings, "AI_PROVIDER", "ollama").lower()

        if provider == "openai":
            text, model = cls._generate_openai(prompt)
        elif provider == "ollama":
            text, model = cls._generate_ollama(prompt)
        else:
            raise AIServiceError("Неизвестный AI_PROVIDER. Допустимы: ollama или openai.")

        cleaned_text = cls.clean_description(text)
        if not cleaned_text:
            raise AIServiceError("ИИ вернул пустой ответ. Попробуйте ещё раз.")
        return {"content": cleaned_text, "prompt": prompt, "provider": provider, "model": model}

    @classmethod
    def _generate_ollama(cls, prompt):
        url = getattr(settings, "OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
        model = getattr(settings, "OLLAMA_MODEL", "qwen2.5:3b")
        try:
            response = requests.post(
                url,
                json={"model": model, "system": cls.SYSTEM_PROMPT, "prompt": prompt, "stream": False},
                timeout=120,
            )
            response.raise_for_status()
            return response.json().get("response", ""), model
        except requests.exceptions.ConnectionError as error:
            raise AIServiceError("Ollama недоступна. Запустите `ollama serve` и проверьте модель.") from error
        except requests.RequestException as error:
            raise AIServiceError(f"Ошибка Ollama: {error}") from error

    @classmethod
    def _generate_openai(cls, prompt):
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        model = getattr(settings, "OPENAI_MODEL", "gpt-4.1-mini")
        if not api_key:
            raise AIServiceError("Для OpenAI задайте переменную окружения OPENAI_API_KEY.")
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "instructions": cls.SYSTEM_PROMPT, "input": prompt, "store": False},
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            text = data.get("output_text", "")
            if not text:
                text = "".join(
                    part.get("text", "")
                    for item in data.get("output", [])
                    for part in item.get("content", [])
                    if part.get("type") == "output_text"
                )
            return text, model
        except requests.RequestException as error:
            raise AIServiceError(f"Ошибка OpenAI API: {error}") from error

    @classmethod
    def generate_description(cls, property):
        """Compatibility method for integrations created before the assistant screen."""
        return cls.generate_content(property, "full_description", "business")["content"]
