from django import template


register = template.Library()


@register.filter
def attr(_form, value):
    """Build a form field id from a stable prefix in compact template loops."""
    return value
