"""Prepare public landing copy without repeated prices or placeholder claims."""

import re

from properties.models import default_realtor_benefits
from properties.services.property_marketing import format_number


DEMO_TRUST_PLACEHOLDER = "Демо-профиль показывает, как выглядит доверительный блок риелтора."


def landing_heading(property):
    candidates = (property.landing_title, property.marketing_headline, property.title)
    price = format_number(property.price) if property.price else ""
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(property.currency, property.currency)
    price_pattern = None
    if price and symbol:
        grouped_number = re.escape(price).replace(r"\ ", r"\s*")
        price_pattern = re.compile(rf"(?<!\d){grouped_number}\s*{re.escape(symbol)}(?:\s*/\s*мес\.?)?(?!\w)", re.I)

    for value in candidates:
        heading = (value or "").strip()
        if not heading:
            continue
        if price_pattern:
            heading = price_pattern.sub("", heading)
            heading = re.sub(r"\s+(?:за|по цене)\s*$", "", heading, flags=re.I)
            heading = heading.strip(" \t\n—–-,:;")
            heading = re.sub(r"\s{2,}", " ", heading)
        if heading:
            return heading
    return property.get_property_type_display()


def authored_benefits(items):
    placeholders = {
        (item["title"], item["description"])
        for item in default_realtor_benefits()
    }
    benefits = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        if not title or not description or title == "Преимущество" or (title, description) in placeholders:
            continue
        benefits.append({"icon": item.get("icon") or "◆", "title": title, "description": description})
    return benefits


def landing_trust_copy(property, profile):
    about = (property.landing_trust_about or "").strip()
    if about == DEMO_TRUST_PLACEHOLDER:
        about = ""
    if not about and profile:
        about = (profile.about or "").strip()
    benefits = authored_benefits(property.landing_benefits)
    if not benefits and profile:
        benefits = authored_benefits(profile.benefits)
    realtor_visible = bool(profile and any((profile.photo, profile.display_name, profile.phone, profile.telegram_username, profile.email)))
    return {
        "landing_trust_about": about,
        "landing_trust_benefits": benefits,
        "landing_realtor_visible": realtor_visible,
        "landing_trust_visible": bool(about or benefits or realtor_visible),
    }
