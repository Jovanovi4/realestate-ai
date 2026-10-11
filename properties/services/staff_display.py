"""Readable staff labels for CRM screens and selection forms."""

import re


def staff_display_name(user):
    if user is None:
        return "Не назначен"
    profile = getattr(user, "realtor_profile", None)
    name = (getattr(profile, "display_name", "") or user.get_full_name()).strip()
    if name:
        return name
    username = user.username.strip()
    if username and not (
        re.fullmatch(r"[+()\d\s.\-]+", username)
        and sum(character.isdigit() for character in username) >= 7
    ):
        return username
    return f"Риелтор №{user.pk}"
