import re
import time

import requests
from django.conf import settings
from billing.usage import record_provider

from .property_marketing import price_summary, property_fact_rows


class AIServiceError(Exception):
    def __init__(self, message, *, outcome_unknown=False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class AIService:
    """Provider-neutral generation service for marketing copy based on card data."""

    SYSTEM_PROMPT = """Ты ИИ-помощник риелтора. Пиши только на русском языке.
Для описания недвижимости используй исключительно факты из блока «Данные объекта».
Для текста о работе с клиентом используй только блок «Возможности сервиса».
Никогда не добавляй,
не предполагай и не приукрашивай характеристики, инфраструктуру, район, вид,
транспортную доступность, юридический статус, ремонт или любые другие сведения.
Если факта нет в данных, не упоминай его. Не используй Markdown, звёздочки,
решётки, списки или служебные комментарии. Верни только готовый текст.
Не повторяй факты и не растягивай текст ради объёма.
Если в запросе явно требуются маркеры вида [[name]] для комплекта текстов,
сохрани только эти маркеры и размести после каждого готовый текст."""

    TONE_INSTRUCTIONS = {
        "business": "Тон: деловой, ясный и профессиональный.",
        "premium": "Тон: сдержанно-премиальный, без неподтверждённых превосходных степеней.",
        "concise": "Тон: лаконичный, без воды.",
        "emotional": "Тон: тёплый и эмоциональный, но без выдумывания фактов.",
    }

    SERVICE_FACTS = """Возможности сервиса:
- Клиент может задать вопросы об объекте.
- Клиент может оставить заявку и согласовать просмотр.
- Риелтор может уточнить условия продажи или аренды."""

    CONTENT_INSTRUCTIONS = {
        "headline": "Создай один заголовок объявления длиной до 90 символов. Не добавляй кавычки.",
        "short_description": "Создай короткое описание: один абзац, до 450 символов. Если фактов мало, пиши короче.",
        "full_description": "Создай полное описание: 1–3 коротких абзаца, до 1 500 символов. Используй все значимые факты без повторов.",
        "landing_headline": "Создай заголовок первого экрана лендинга до 90 символов.",
        "landing_subtitle": "Создай подзаголовок лендинга: один короткий абзац до 280 символов.",
        "landing_about": "Создай текст для блока «Об объекте»: 1–2 абзаца до 900 символов.",
        "seo_title": "Создай SEO-заголовок страницы до 60 символов.",
        "seo_description": "Создай SEO-описание до 155 символов.",
        "benefits": "Для блока «Почему выбирают нас» создай 1–2 коротких предложения о работе с клиентом. Используй только «Возможности сервиса». Не перечисляй характеристики объекта и не утверждай ничего об опыте риелтора.",
        "cta": "Создай один короткий призыв оставить заявку или записаться на просмотр.",
        "lead_reply": "Создай короткий профессиональный ответ клиенту до 500 символов.",
    }

    INLINE_FIELD_INSTRUCTIONS = {
        "marketing_headline": ("headline", "Создай один заголовок объявления длиной до 90 символов. Не добавляй кавычки."),
        "short_description": ("short_description", "Создай короткое описание: один абзац, до 450 символов. Если фактов мало, пиши короче."),
        "description": ("full_description", "Создай полное описание: 1–3 коротких абзаца, до 1 500 символов. Используй все значимые факты без повторов."),
        "landing_title": ("landing_headline", "Создай заголовок первого экрана лендинга до 90 символов."),
        "landing_subtitle": ("landing_subtitle", "Создай подзаголовок лендинга: один короткий абзац до 280 символов."),
        "landing_about_title": ("landing_about", "Создай короткий заголовок для блока «Об объекте» до 70 символов."),
        "landing_contact_title": ("cta", "Создай один короткий призыв оставить заявку или записаться на просмотр."),
        "landing_trust_about": ("benefits", "Для блока «Почему выбирают нас» создай 1–2 коротких предложения о работе с клиентом. Используй только «Возможности сервиса». Не перечисляй характеристики объекта и не утверждай ничего об опыте риелтора."),
        "seo_title": ("seo_title", "Создай SEO-заголовок страницы до 60 символов."),
        "seo_description": ("seo_description", "Создай SEO-описание до 155 символов."),
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
        ]
        price = price_summary(property)
        if price:
            facts.append(("Цена", price.main))
            facts.extend((label, value) for label, value in (
                ("Единица цены", price.scope), ("Цена за м²", price.per_sqm),
            ) if value)
        facts.extend([
            ("Адрес", property.address),
            *property_fact_rows(property),
            ("Год постройки", property.year_built),
            ("Особенности и удобства", property.amenities),
            ("Комиссия, %", property.rent_commission if property.deal_type == "rent" else ""),
            ("Коммунальные платежи", property.get_utilities_terms_display() if property.deal_type == "rent" and property.utilities_terms else ""),
        ])
        if property.deal_type == "rent" and not property.is_commercial:
            facts.extend([
                ("Можно с животными", "да" if property.pets_allowed else ""),
                ("Можно с детьми", "да" if property.children_allowed else ""),
            ])
        return "\n".join(f"- {label}: {value}" for label, value in facts if value not in (None, ""))

    @staticmethod
    def _deal_instruction(property):
        if property.is_commercial:
            if property.deal_type == "rent":
                return (
                    "Это аренда коммерческого помещения. Цена указана за всё помещение в месяц; "
                    "ставка за м² только расчётная. Описывай назначение, площадь, доступ и условия аренды "
                    "только при наличии фактов. Не используй формулировки о проживании, жильцах, "
                    "детях, домашних животных или ипотеке. Не называй объект продажей."
                )
            return (
                "Это продажа коммерческого помещения. Цена указана за весь объект; ставка за м² "
                "только расчётная. Описывай деловое назначение и характеристики помещения "
                "только при наличии фактов. Не используй формулировки о проживании, жильцах или ипотеке."
            )
        if property.deal_type == "rent":
            return (
                "Это объявление о долгосрочной аренде. Акцентируй только известные условия проживания, "
                "срок, доступность, мебель, коммунальные платежи и правила. Не называй объект продажей."
            )
        return "Это объявление о продаже."

    @classmethod
    def _content_instruction(cls, property, content_type):
        if property.is_commercial and content_type == "cta":
            return "Создай один короткий призыв уточнить условия коммерческого помещения или договориться об осмотре."
        return cls.CONTENT_INSTRUCTIONS[content_type]

    @classmethod
    def build_prompt(cls, property, content_type, tone):
        if content_type not in cls.CONTENT_INSTRUCTIONS or tone not in cls.TONE_INSTRUCTIONS:
            raise AIServiceError("Неизвестный тип текста или тон генерации.")
        prompt_parts = (
            cls._content_instruction(property, content_type),
            cls.TONE_INSTRUCTIONS[tone],
            cls._deal_instruction(property),
            cls.SERVICE_FACTS if content_type == "benefits" else "",
            "Данные объекта:\n" + cls._property_facts(property),
        )
        return "\n\n".join(part for part in prompt_parts if part)

    @classmethod
    def generate_content(cls, property, content_type, tone):
        prompt = cls.build_prompt(property, content_type, tone)
        return cls._run_prompt(prompt)

    @classmethod
    def generate_inline_content(cls, property, target, tone):
        """Generate copy for a concrete editor field without exposing prompt details to the UI."""
        content_type, instruction = cls.INLINE_FIELD_INSTRUCTIONS.get(target, (None, None))
        is_benefit_field = target.startswith("landing_benefit_") and (
            target.endswith("_title") or target.endswith("_description")
        )
        if (not content_type and not is_benefit_field) or tone not in cls.TONE_INSTRUCTIONS:
            raise AIServiceError("Неизвестное поле или тон генерации.")
        if is_benefit_field and target.endswith("_title"):
            content_type = "benefits"
            instruction = "Создай короткий заголовок одной карточки преимущества риелтора, до 80 символов. Не добавляй нумерацию."
        elif is_benefit_field:
            content_type = "benefits"
            instruction = "Создай описание одной карточки преимущества риелтора: одно предложение до 180 символов. Не добавляй заголовок или нумерацию."
        elif property.is_commercial and target == "landing_contact_title":
            instruction = cls._content_instruction(property, "cta")
        prompt_parts = (
            instruction,
            cls.TONE_INSTRUCTIONS[tone],
            cls._deal_instruction(property),
            cls.SERVICE_FACTS if content_type == "benefits" else "",
            "Данные объекта:\n" + cls._property_facts(property),
        )
        prompt = "\n\n".join(part for part in prompt_parts if part)
        return content_type, cls._run_prompt(prompt)

    @classmethod
    def _run_prompt(cls, prompt):
        provider = getattr(settings, "AI_PROVIDER", "openai").lower()
        started_at = time.monotonic()
        record_provider(provider=provider, provider_status="started")

        if provider == "openai":
            text, model = cls._generate_openai(prompt)
        elif provider == "ollama":
            text, model = cls._generate_ollama(prompt)
        else:
            record_provider(provider_status="not_sent")
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
            # The landing's «Об объекте» section uses the same description field.
            "package": ["headline", "short_description", "full_description", "landing_headline", "landing_subtitle", "seo_title", "seo_description", "benefits", "cta"],
            "landing": ["landing_headline", "landing_subtitle", "landing_about", "seo_title", "seo_description", "benefits", "cta"],
        }
        content_types = bundles.get(bundle_type)
        if not content_types:
            raise AIServiceError("Неизвестный комплект генерации.")
        sections = "\n".join(
            f"[[{content_type}]] — {cls._content_instruction(property, content_type)}" for content_type in content_types
        )
        prompt = "\n\n".join((
            "Создай комплект текстов. Для каждого пункта верни результат строго после его маркера. Не пропускай маркеры.",
            sections,
            cls.TONE_INSTRUCTIONS[tone],
            cls._deal_instruction(property),
            cls.SERVICE_FACTS,
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
        prompt = "\n\n".join((
            labels[improvement], cls.TONE_INSTRUCTIONS[tone], cls._deal_instruction(property),
            "Исходный текст:\n" + source_text, "Данные объекта:\n" + cls._property_facts(property),
        ))
        return cls._run_prompt(prompt)

    @classmethod
    def generate_lead_reply(cls, property, lead, tone):
        prompt = "\n\n".join((
            "Подготовь первый ответ риелтора на заявку. Поздоровайся по имени, поблагодари за интерес, ответь нейтрально и предложи уточнить удобное время для связи. Не обещай и не утверждай ничего, чего нет в данных.",
            cls.TONE_INSTRUCTIONS[tone],
            cls._deal_instruction(property),
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
        record_provider(provider="ollama", provider_model=model)
        try:
            response = requests.post(
                url,
                json={"model": model, "system": cls.SYSTEM_PROMPT, "prompt": prompt, "stream": False},
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            input_tokens, output_tokens = data.get("prompt_eval_count"), data.get("eval_count")
            total = input_tokens + output_tokens if type(input_tokens) is int and type(output_tokens) is int else None
            record_provider(provider_status="completed", provider_model=data.get("model", model), input_tokens=input_tokens,
                output_tokens=output_tokens, cached_input_tokens=data.get("prompt_eval_cached_count"), total_tokens=total)
            if data.get("done", True) is not True:
                raise AIServiceError("Ollama не завершила генерацию.", outcome_unknown=True)
            return data.get("response", ""), model
        except requests.exceptions.ConnectionError as error:
            record_provider(provider_status="unknown")
            raise AIServiceError("Ollama недоступна. Запустите `ollama serve` и проверьте модель.", outcome_unknown=True) from error
        except requests.Timeout as error:
            record_provider(provider_status="unknown")
            raise AIServiceError("Ollama не ответила вовремя. Исход запроса требует проверки.", outcome_unknown=True) from error
        except requests.RequestException as error:
            record_provider(provider_status="failed")
            raise AIServiceError(f"Ошибка Ollama: {error}") from error
        except (ValueError, AttributeError, TypeError) as error:
            record_provider(provider_status="unknown")
            raise AIServiceError("Ollama вернула некорректный ответ.", outcome_unknown=True) from error

    @classmethod
    def _generate_openai(cls, prompt):
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        model = getattr(settings, "OPENAI_MODEL", "gpt-6-luna")
        record_provider(provider="openai", provider_model=model)
        if not api_key:
            record_provider(provider_status="not_sent")
            raise AIServiceError("Для OpenAI задайте переменную окружения OPENAI_API_KEY.")
        try:
            payload = {"model": model, "instructions": cls.SYSTEM_PROMPT, "input": prompt, "store": False}
            reasoning_effort = getattr(settings, "OPENAI_REASONING_EFFORT", "") or ("none" if model == "gpt-6-luna" else "")
            if reasoning_effort:
                payload["reasoning"] = {"effort": reasoning_effort}
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            details = usage.get("input_tokens_details") or {}
            record_provider(provider_model=data.get("model", model), provider_response_id=data.get("id", ""),
                provider_status=data.get("status", "completed"), input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"), cached_input_tokens=details.get("cached_tokens"), total_tokens=usage.get("total_tokens"))
            if data.get("status", "completed") != "completed":
                raise AIServiceError("OpenAI не завершил генерацию. Готовый текст не сохранён.", outcome_unknown=data.get("status") in {"queued", "in_progress"})
            text = data.get("output_text", "")
            if not text:
                text = "".join(
                    part.get("text", "")
                    for item in data.get("output", [])
                    for part in item.get("content", [])
                    if part.get("type") == "output_text"
                )
            return text, model
        except (requests.Timeout, requests.ConnectionError) as error:
            record_provider(provider_status="unknown")
            raise AIServiceError("OpenAI не ответил вовремя или соединение прервано. Исход запроса требует проверки.", outcome_unknown=True) from error
        except requests.RequestException as error:
            record_provider(provider_status="failed")
            raise AIServiceError(f"Ошибка OpenAI API: {error}") from error
        except (ValueError, AttributeError, TypeError) as error:
            record_provider(provider_status="unknown")
            raise AIServiceError("OpenAI вернул некорректный ответ.", outcome_unknown=True) from error

    @classmethod
    def generate_description(cls, property):
        """Compatibility method for integrations created before the assistant screen."""
        return cls.generate_content(property, "full_description", "business")["content"]
