from django import template

from properties.services.property_marketing import format_money, price_summary
from properties.services.staff_display import staff_display_name


register = template.Library()


@register.filter
def money(value, currency=""):
    return format_money(value, currency)


@register.filter
def property_price(property):
    summary = price_summary(property)
    return summary.main if summary else ""


@register.filter
def property_rate(property):
    summary = price_summary(property)
    return summary.per_sqm if summary else ""


@register.filter
def staff_name(user):
    return staff_display_name(user)
