"""Read a dedicated IMAP folder into the CRM review queue, without deleting mail."""

from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime
from hashlib import sha256
import imaplib
import re

from django.db import transaction
from django.utils import timezone
from django.utils.html import strip_tags

from ..models import InboundEvent, InboundMailboxCursor


def parse_email(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender_name, sender_address = parseaddr(str(message.get("from", "")))
    subject = str(message.get("subject", ""))[:255]
    part = message.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    if part and part.get_content_type() == "text/html":
        body = strip_tags(body)
    received_at = None
    try:
        received_at = parsedate_to_datetime(str(message.get("date", "")))
        if received_at and timezone.is_naive(received_at):
            received_at = timezone.make_aware(received_at)
    except (TypeError, ValueError, IndexError):
        pass
    return {
        "sender": f"{sender_name} <{sender_address}>"[:255] if sender_name else sender_address[:255],
        "subject": subject,
        "body": str(body)[:20000],
        "received_at": received_at or timezone.now(),
    }


def imap_received_at(response):
    """Prefer the server's arrival time to the sender-controlled Date header."""
    for item in response:
        if isinstance(item, tuple) and isinstance(item[0], bytes):
            match = re.search(rb'INTERNALDATE "([^"]+)"', item[0])
            if match:
                try:
                    return datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
                except (ValueError, UnicodeDecodeError):
                    pass
    return None


def import_mailbox(*, link, host, username, password, folder, limit=100, imap_factory=imaplib.IMAP4_SSL):
    """Resume by IMAP UIDVALIDITY+UID; processing the same delivery is harmless."""
    if not link.enabled or (link.agency_id and link.agency.closed_at):
        raise ValueError("Приём обращений в пространстве отключён.")
    with imap_factory(host, 993, timeout=20) as mailbox:
        mailbox.login(username, password)
        status, _ = mailbox.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError("Не удалось открыть папку входящих обращений.")
        _, validity_data = mailbox.response("UIDVALIDITY")
        validity = validity_data[0].decode("ascii") if validity_data and validity_data[0] else ""
        if not validity.isdigit():
            raise RuntimeError("Сервер IMAP не вернул UIDVALIDITY.")
        cursor, _ = InboundMailboxCursor.objects.get_or_create(
            link=link, mailbox=username.casefold(), folder=folder,
        )
        if cursor.uid_validity != validity:
            cursor.uid_validity = validity
            cursor.last_uid = 0
            cursor.save(update_fields=["uid_validity", "last_uid", "updated_at"])
        criterion = f"UID {cursor.last_uid + 1}:*" if cursor.last_uid else "ALL"
        status, matches = mailbox.uid("SEARCH", None, criterion)
        if status != "OK":
            raise RuntimeError("Не удалось получить список писем IMAP.")
        uids = [int(value) for value in (matches[0] or b"").split() if int(value) > cursor.last_uid]
        imported = 0
        for uid in uids[:limit]:
            status, response = mailbox.uid("FETCH", str(uid), "(INTERNALDATE BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError(f"Не удалось прочитать письмо UID {uid}.")
            raw = next((item[1] for item in response if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
            if raw is None:
                raise RuntimeError(f"Письмо UID {uid} не содержит текста.")
            fields = parse_email(raw)
            fields["received_at"] = imap_received_at(response) or fields["received_at"]
            identity = f"{username.casefold()}:{folder}:{validity}:{uid}".encode("utf-8")
            with transaction.atomic():
                event, created = InboundEvent.objects.get_or_create(
                    link=link, channel="email", external_id=f"imap:{sha256(identity).hexdigest()}",
                    defaults=fields,
                )
                InboundMailboxCursor.objects.filter(pk=cursor.pk, last_uid__lt=uid).update(last_uid=uid)
            if created:
                imported += 1
            cursor.last_uid = uid
        return imported
