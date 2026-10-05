"""Validated CSV import into the current CRM workspace."""

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import StringIO

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from ..agency_access import agency_for, workspace_queryset
from ..forms import normalize_phone
from ..models import Client


MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 1_000
HEADERS = {
    "имя": "name", "name": "name",
    "телефон": "phone", "phone": "phone",
    "статус": "status", "status": "status", "отношения": "status",
    "источник": "source", "source": "source",
    "бюджет": "budget", "budget": "budget",
    "район": "preferred_area", "preferred_area": "preferred_area",
    "удобное время": "preferred_contact_time", "preferred_contact_time": "preferred_contact_time",
    "комментарий": "notes", "notes": "notes",
}


class ClientImportError(Exception):
    def __init__(self, message, row_errors=()):
        super().__init__(message)
        self.row_errors = tuple(row_errors)


@dataclass(frozen=True)
class ClientImportResult:
    created: int
    skipped: int


def canonical_phone(value):
    phone = normalize_phone(value)
    digits = phone.lstrip("+")
    if len(digits) == 11 and digits[0] in "78":
        return f"+7{digits[1:]}"
    return phone


def _choice(value, choices, default):
    if not value:
        return default
    matches = {str(key).casefold(): key for key, _ in choices}
    matches.update({label.casefold(): key for key, label in choices})
    result = matches.get(value.casefold())
    if result is None:
        raise ValueError("неизвестное значение статуса или источника")
    return result


def _client_relationship(value):
    if not value:
        return "new", ""
    normalized = value.casefold()
    current = {str(key).casefold(): key for key, _ in Client.STATUS_CHOICES}
    current.update({label.casefold(): key for key, label in Client.STATUS_CHOICES})
    if normalized in current:
        return current[normalized], ""
    legacy = {str(key).casefold(): key for key, _ in Client.LEGACY_STATUS_CHOICES}
    legacy.update({label.casefold(): key for key, label in Client.LEGACY_STATUS_CHOICES})
    previous = legacy.get(normalized)
    if previous:
        return {"viewing": "in_progress", "negotiation": "in_progress", "won": "nurture", "lost": "inactive"}[previous], previous
    raise ValueError("неизвестное значение статуса или отношений")


def _decode(upload):
    data = upload.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise ClientImportError("Файл слишком большой. Максимум — 1 МБ и 1000 строк.")
    if not data:
        raise ClientImportError("Файл пустой.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1251")
        except UnicodeDecodeError as error:
            raise ClientImportError("Не удалось прочитать CSV. Используйте UTF-8 или Windows-1251.") from error


def _rows(upload):
    text = _decode(upload)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;")
        reader = csv.reader(StringIO(text, newline=""), dialect=dialect, strict=True)
    except csv.Error:
        delimiter = ";" if text.partition("\n")[0].count(";") > text.partition("\n")[0].count(",") else ","
        reader = csv.reader(StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        headings = next(reader)
    except (StopIteration, csv.Error) as error:
        raise ClientImportError("Не удалось прочитать заголовки CSV.") from error
    columns = [HEADERS.get(heading.strip().casefold()) for heading in headings]
    if len(headings) != len(set(heading.strip().casefold() for heading in headings)) or len([name for name in columns if name]) != len(set(name for name in columns if name)):
        raise ClientImportError("В CSV повторяются названия столбцов.")
    if not {"name", "phone"}.issubset(columns):
        raise ClientImportError("Нужны столбцы «Имя» и «Телефон».")

    parsed = []
    errors = []
    try:
        for line_number, values in enumerate(reader, start=2):
            if not any(value.strip() for value in values):
                continue
            if len(parsed) + len(errors) >= MAX_IMPORT_ROWS:
                raise ClientImportError("В одном файле может быть не более 1000 контактов.")
            if len(values) != len(headings):
                errors.append(f"Строка {line_number}: число полей не совпадает с заголовками.")
                continue
            row = {name: value.strip() for name, value in zip(columns, values) if name}
            try:
                if not row["name"]:
                    raise ValueError("не указано имя")
                row["phone"] = canonical_phone(row["phone"])
                row["status"], row["legacy_status"] = _client_relationship(row.get("status", ""))
                row["source"] = _choice(row.get("source", ""), Client.SOURCE_CHOICES, "other")
                raw_budget = row.get("budget", "").replace(" ", "").replace("\u00a0", "").replace(",", ".")
                row["budget"] = Decimal(raw_budget) if raw_budget else None
                if row["budget"] is not None and (not row["budget"].is_finite() or row["budget"] < 0):
                    raise ValueError("некорректный бюджет")
            except (ValidationError, ValueError, InvalidOperation) as error:
                message = error.messages[0] if isinstance(error, ValidationError) else str(error)
                errors.append(f"Строка {line_number}: {message}.")
                continue
            parsed.append((line_number, row))
    except csv.Error as error:
        raise ClientImportError("CSV содержит незакрытые кавычки или повреждённую строку.") from error
    return parsed, errors


def import_clients(upload, actor):
    """Create valid new contacts; reject invalid files without partial writes."""
    rows, errors = _rows(upload)
    if not rows and not errors:
        raise ClientImportError("В файле нет контактов.")
    agency = agency_for(actor)
    existing = set()
    for phone in workspace_queryset(Client.objects, actor).values_list("phone", flat=True):
        try:
            existing.add(canonical_phone(phone))
        except ValidationError:
            continue

    candidates = []
    skipped = 0
    for line_number, row in rows:
        if row["phone"] in existing:
            skipped += 1
            continue
        existing.add(row["phone"])
        client = Client(
            owner=actor, agency=agency, name=row["name"], phone=row["phone"],
            status=row["status"], legacy_status=row["legacy_status"], source=row["source"], budget=row["budget"],
            preferred_area=row.get("preferred_area", ""),
            preferred_contact_time=row.get("preferred_contact_time", ""),
            notes=row.get("notes", ""),
        )
        try:
            client.full_clean()
        except ValidationError as error:
            errors.append(f"Строка {line_number}: {error.messages[0]}.")
        else:
            candidates.append(client)
    if errors:
        raise ClientImportError(
            f"Импорт не выполнен: ошибок — {len(errors)}. Показаны первые 10; исправьте файл и загрузите снова.",
            errors[:10],
        )

    created = 0
    with transaction.atomic():
        for client in candidates:
            try:
                with transaction.atomic():
                    client.save()
            except IntegrityError:
                scope = Client.objects.filter(agency=agency, phone=client.phone)
                if agency is None:
                    scope = scope.filter(owner=actor)
                if not scope.exists():
                    raise
                skipped += 1
            else:
                created += 1
    return ClientImportResult(created=created, skipped=skipped)
