"""BP-0.1 definitions, not live enforcement or a payment price list."""

from copy import deepcopy

from django.core.exceptions import ValidationError


POLICY_VERSION = "BP-0.1"
AI_COSTS = {
    "short_text": 1, "improve": 1, "lead_reply": 1,
    "full_description": 2, "package": 3, "landing": 5, "audit": 0,
}
LIMIT_KEYS = {
    "active_properties", "total_properties", "landings", "seats",
    "storage_bytes", "ai_credits", "demo_ai_credits",
}
DEFAULT_PLANS = {
    "demo": {"name": "Демо", "active_properties": 2, "total_properties": 50,
             "landings": 1, "seats": 1, "storage_bytes": 200 * 1024**2,
             "ai_credits": 0, "demo_ai_credits": 5, "xml_export": False, "team": False},
    "standard": {"name": "Стандарт", "active_properties": 20, "total_properties": 1000,
                 "landings": 10, "seats": 1, "storage_bytes": 2 * 1024**3,
                 "ai_credits": 100, "demo_ai_credits": 0, "xml_export": True, "team": False},
    "business": {"name": "Бизнес", "active_properties": 100, "total_properties": 5000,
                 "landings": 50, "seats": 5, "storage_bytes": 10 * 1024**3,
                 "ai_credits": 500, "demo_ai_credits": 0, "xml_export": True, "team": True},
}


def default_ai_costs():
    return deepcopy(AI_COSTS)


def validate_ai_costs(value):
    if not isinstance(value, dict) or set(value) != set(AI_COSTS):
        raise ValidationError("Нужен полный набор известных операций ИИ.")
    if any(type(cost) is not int or cost < 0 for cost in value.values()):
        raise ValidationError("Стоимость ИИ должна быть целым неотрицательным числом.")
    if value["audit"] != 0 or any(value[key] < 1 for key in value if key != "audit"):
        raise ValidationError("Локальная проверка бесплатна; генерации требуют кредиты.")


def validate_snapshot(value):
    try:
        if not isinstance(value, dict) or type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError
        if value["plan_code"] not in DEFAULT_PLANS or not isinstance(value["policy_version"], str) or not value["policy_version"].strip():
            raise ValueError
        if type(value["version"]) is not int or value["version"] < 1:
            raise ValueError
        limits = value["limits"]
        if not isinstance(limits, dict) or set(limits) != LIMIT_KEYS or any(type(v) is not int or v < 0 for v in limits.values()):
            raise ValueError
        if limits["seats"] < 1 or limits["total_properties"] < limits["active_properties"] or limits["landings"] > limits["active_properties"]:
            raise ValueError
        if type(value["grace_days"]) is not int or value["grace_days"] < 0:
            raise ValueError
        if not isinstance(value["features"], dict) or set(value["features"]) != {"xml_export", "team"} or any(type(v) is not bool for v in value["features"].values()):
            raise ValueError
        validate_ai_costs(value["ai_costs"])
    except (KeyError, TypeError, ValueError):
        raise ValidationError("Некорректный снимок условий тарифа.") from None
