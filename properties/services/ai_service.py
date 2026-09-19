import re
import time

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
решётки, списки или служебные комментарии. Верни только готовый текст.
Если в запросе явно требуются маркеры вида [[name]] для комплекта текстов,
сохрани только эти маркеры и размести после каждого готовый текст."""

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
        "landing_headline": "Создай заголовок первого экрана лендинга до 90 символов.",
        "landing_subtitle": "Создай подзаголовок лендинга: один короткий абзац до 280 символов.",
        "landing_about": "Создай текст для блока «Об объекте»: 1–2 абзаца до 900 символов.",
        "seo_title": "Создай SEO-заголовок страницы до 60 символов.",
        "seo_description": "Создай SEO-описание до 155 символов.",
        "benefits": "Создай три коротких преимущества объекта одной строкой через точку с запятой. Используй только известные факты.",
        "cta": "Создай один короткий призыв оставить заявку или записаться на просмотр.",
        "lead_reply": "Создай короткий профессиональный ответ клиенту до 500 символов.",
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
            ("Тип сделки", property.get_deal_type_display()),
            ("Тип", property.get_property_type_display()),
            ("Статус", property.get_status_display()),
            ("Ставка аренды в месяц" if property.deal_type == "rent" else "Цена", f"{property.price} {property.get_currency_display()}" if property.price else ""),
            ("Адрес", property.address),
            ("Площадь", f"{property.area} м²" if property.area else ""),
            ("Комнаты", property.rooms),
            ("Санузлы", property.bathrooms),
            ("Этаж", f"{property.floor} из {property.floors_total}" if property.floor and property.floors_total else property.floor),
            ("Площадь участка", f"{property.land_area} м²" if property.land_area else ""),
            ("Год постройки", property.year_built),
            ("Состояние", property.get_condition_display() if property.condition else ""),
            ("Особенности и удобства", property.amenities),
            ("Залог", property.rent_deposit if property.deal_type == "rent" else ""),
            ("Комиссия, %", property.rent_commission if property.deal_type == "rent" else ""),
            ("Коммунальные платежи", property.get_utilities_terms_display() if property.deal_type == "rent" and property.utilities_terms else ""),
            ("Минимальный срок аренды, мес.", property.min_lease_months if property.deal_type == "rent" else ""),
            ("Свободен с", property.available_from if property.deal_type == "rent" else ""),
            ("Мебель", "есть" if property.deal_type == "rent" and property.furnished else ""),
            ("Можно с животными", "да" if property.deal_type == "rent" and property.pets_allowed else ""),
            ("Можно с детьми", "да" if property.deal_type == "rent" and property.children_allowed else ""),
        ]
        return "\n".join(f"- {label}: {value}" for label, value in facts if value not in (None, ""))

    @classmethod
    def build_prompt(cls, property, content_type, tone):
        if content_type not in cls.CONTENT_INSTRUCTIONS or tone not in cls.TONE_INSTRUCTIONS:
            raise AIServiceError("Неизвестный тип текста или тон генерации.")
        deal_instruction = "Это объявление о долгосрочной аренде. Акцентируй только известные условия проживания, срок, доступность, мебель, коммунальные платежи и правила. Не называй объект продажей." if property.deal_type == "rent" else "Это объявление о продаже."
        return "\n\n".join((
            cls.CONTENT_INSTRUCTIONS[content_type],
            cls.TONE_INSTRUCTIONS[tone],
            deal_instruction,
            "Данные объекта:\n" + cls._property_facts(property),
        ))

    @classmethod
    def generate_content(cls, property, content_type, tone):
        prompt = cls.build_prompt(property, content_type, tone)
        return cls._run_prompt(prompt)

    @classmethod
    def _run_prompt(cls, prompt):
        provider = getattr(settings, "AI_PROVIDER", "ollama").lower()
        started_at = time.monotonic()

        if provider == "openai":
            text, model = cls._generate_openai(prompt)
        elif provider == "ollama":
            text, model = cls._generate_ollama(prompt)
        else:
            raise AIServiceError("Неизвестный AI_PROVIDER. Допустимы: ollama или openai.")

        cleaned_text = cls.clean_description(text)
        if not cleaned_text:
            raise AIServiceError("ИИ вернул пустой ответ. Попробуйте ещё раз.")
        return {
            "content": cleaned_text,
            "prompt": prompt,
            "provider": provider,
            "model": model,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }

    @classmethod
    def generate_bundle(cls, property, tone, bundle_type):
        bundles = {
            "package": ["headline", "short_description", "full_description"],
            "landing": ["landing_headline", "landing_subtitle", "landing_about", "seo_title", "seo_description", "benefits", "cta"],
        }
        content_types = bundles.get(bundle_type)
        if not content_types:
            raise AIServiceError("Неизвестный комплект генерации.")
        sections = "\n".join(f"[[{content_type}]] — {cls.CONTENT_INSTRUCTIONS[content_type]}" for content_type in content_types)
        prompt = "\n\n".join((
            "Создай комплект текстов. Для каждого пункта верни результат строго после его маркера. Не пропускай маркеры.",
            sections,
            cls.TONE_INSTRUCTIONS[tone],
            "Это долгосрочная аренда; используй условия аренды." if property.deal_type == "rent" else "Это продажа объекта.",
            "Данные объекта:\n" + cls._property_facts(property),
        ))
        result = cls._run_prompt(prompt)
        pattern = r"\[\[([a-z_]+)\]\]\s*"
        matches = list(re.finditer(pattern, result["content"]))
        parsed = {}
        for index, match in enumerate(matches):
            key = match.group(1)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(result["content"])
            if key in content_types:
                parsed[key] = cls.clean_description(result["content"][match.end():end])
        if not all(parsed.get(content_type) for content_type in content_types):
            raise AIServiceError("ИИ вернул неполный комплект. Попробуйте ещё раз.")
        return [{**result, "content": parsed[content_type], "content_type": content_type} for content_type in content_types]

    @classmethod
    def improve_text(cls, property, content_type, tone, improvement, source_text):
        if not source_text.strip():
            raise AIServiceError("Вставьте текст, который нужно улучшить.")
        labels = {
            "shorter": "Сделай текст короче, сохранив все факты.",
            "stronger": "Сделай текст убедительнее, не добавляя фактов.",
            "premium": "Сделай подачу сдержанно-премиальной, без преувеличений.",
            "plain": "Убери канцелярит и сделай текст естественным.",
            "audience": f"Адаптируй текст для аудитории: {source_text.splitlines()[0][:200]}.",
        }
        if improvement not in labels:
            raise AIServiceError("Выберите способ улучшения текста.")
        prompt = "\n\n".join((labels[improvement], cls.TONE_INSTRUCTIONS[tone], "Исходный текст:\n" + source_text, "Данные объекта:\n" + cls._property_facts(property)))
        return cls._run_prompt(prompt)

    @classmethod
    def generate_lead_reply(cls, property, lead, tone):
        prompt = "\n\n".join((
            "Подготовь первый ответ риелтора на заявку. Поздоровайся по имени, поблагодари за интерес, ответь нейтрально и предложи уточнить удобное время для связи. Не обещай и не утверждай ничего, чего нет в данных.",
            cls.TONE_INSTRUCTIONS[tone],
            f"Заявка клиента:\nИмя: {lead.name}\nСообщение: {lead.message or 'не указано'}",
            "Данные объекта:\n" + cls._property_facts(property),
        ))
        return cls._run_prompt(prompt)

    @classmethod
    def audit_property(cls, property, has_contacts):
        checks = [
            ("Название", bool(property.title)), ("Цена", bool(property.price)), ("Адрес", bool(property.address)),
            ("Площадь", bool(property.area or property.land_area)), ("Описание", bool(property.description or property.short_description)),
            ("Фотографии", property.images.exists()), ("Контакты риелтора", has_contacts),
        ]
        missing = [label for label, present in checks if not present]
        content = "Карточка готова к публикации." if not missing else "Перед публикацией рекомендуется заполнить: " + ", ".join(missing) + "."
        return {"content": content, "prompt": "Автоматическая проверка готовности карточки.", "provider": "system", "model": "readiness-check", "duration_ms": 0}

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
