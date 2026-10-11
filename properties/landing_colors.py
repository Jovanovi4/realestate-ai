"""Validated accent colours and readable variants for the three landing themes."""

import re


HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalize_hex(value):
    value = (value or "").strip()
    return value.upper() if HEX_COLOR.fullmatch(value) else None


def _rgb(value):
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def _luminance(value):
    channels = [channel / 255 for channel in _rgb(value)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(left, right):
    lighter, darker = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _mix(value, destination, amount):
    start = _rgb(value)
    end = _rgb(destination)
    channels = [round(a + (b - a) * amount) for a, b in zip(start, end)]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def _readable(value, background, destination, minimum=4.5):
    if contrast(value, background) >= minimum:
        return value
    low, high = 0.0, 1.0
    for _ in range(16):
        middle = (low + high) / 2
        if contrast(_mix(value, destination, middle), background) >= minimum:
            high = middle
        else:
            low = middle
    return _mix(value, destination, high)


def landing_custom_palette(value):
    """Return CSS-safe colours; invalid stored values never reach inline styles."""
    color = normalize_hex(value)
    if not color:
        return None
    light = _readable(color, "#FFFFFF", "#000000")
    dark = _readable(color, "#12110F", "#FFFFFF")
    return {
        "light": light,
        "light_hover": _mix(light, "#000000", 0.15),
        "dark": dark,
        "dark_hover": _mix(dark, "#FFFFFF", 0.15),
    }
