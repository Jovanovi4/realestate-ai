import unicodedata


def spreadsheet_safe_cell(value):
    """Keep untrusted CSV cells as text when opened in Excel (QUOTE_ALL required)."""
    text = "" if value is None else str(value)
    first = unicodedata.normalize("NFKC", text.lstrip())[:1]
    if first in {"=", "+", "-", "@"} or text.startswith(("\t", "\r", "\n")):
        # OWASP's Excel-resistant mitigation: tab inside the quoted CSV cell.
        # This is an export for human viewing, not a lossless machine interchange.
        return "\t" + text
    return text
