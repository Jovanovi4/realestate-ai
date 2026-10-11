"""Consistent property facts and price units for outbound materials."""

from dataclasses import dataclass
from decimal import Decimal


def format_number(value):
    number = Decimal(str(value))
    raw = f"{number:,.2f}".rstrip("0").rstrip(".")
    whole, separator, fractional = raw.partition(".")
    return f"{whole.replace(',', ' ')}{',' + fractional if separator else ''}"


def format_money(value, currency=""):
    if value is None:
        return ""
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{format_number(value)} {symbol}".strip()


@dataclass(frozen=True)
class PriceSummary:
    main: str
    scope: str = ""
    per_sqm: str = ""


def price_summary(property):
    if property.price is None:
        return None
    main = format_money(property.price, property.currency)
    if property.deal_type == "rent":
        main += " / мес."
    if not property.is_commercial:
        return PriceSummary(main=main)
    scope = "Аренда всего помещения за месяц" if property.deal_type == "rent" else "Полная цена помещения"
    rate = property.calculated_price_per_sqm
    per_sqm = f"Расчётно {format_money(rate, property.currency)} / м²" if rate is not None else ""
    if per_sqm and property.deal_type == "rent":
        per_sqm += " / мес."
    return PriceSummary(main=main, scope=scope, per_sqm=per_sqm)


def property_fact_rows(property):
    rows = []
    if property.area:
        rows.append(("Площадь", f"{format_number(property.area)} м²"))
    if property.is_commercial:
        for label, value in (
            ("Назначение", property.commercial_purpose),
            ("Высота потолков", f"{format_number(property.ceiling_height_m)} м" if property.ceiling_height_m else ""),
            ("Мощность", f"{format_number(property.power_kw)} кВт" if property.power_kw else ""),
            ("Вход", property.get_entrance_type_display() if property.entrance_type else ""),
            ("Парковка", property.get_parking_type_display() if property.parking_type else ""),
            ("Режим доступа", property.get_access_mode_display() if property.access_mode else ""),
        ):
            if value:
                rows.append((label, value))
    else:
        if property.rooms:
            rows.append(("Комнаты", str(property.rooms)))
        if property.bathrooms:
            rows.append(("Санузлы", str(property.bathrooms)))
        if property.land_area:
            rows.append(("Участок", f"{format_number(property.land_area)} м²"))
    if property.floor:
        floor = f"{property.floor} / {property.floors_total}" if property.floors_total else str(property.floor)
        rows.append(("Этаж", floor))
    if property.condition:
        rows.append(("Состояние", property.get_condition_display()))
    if property.deal_type == "rent":
        if property.furnished and not property.is_commercial:
            rows.append(("Мебель", "Есть"))
        if property.rent_deposit:
            rows.append(("Залог", format_money(property.rent_deposit, property.currency)))
        if property.min_lease_months:
            rows.append(("Срок аренды", f"от {property.min_lease_months} мес."))
        if property.available_from:
            rows.append(("Свободен с", property.available_from.strftime("%d.%m.%Y")))
    return rows
