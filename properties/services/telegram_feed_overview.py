"""Read-only CRM summary for one Telegram audience."""

from django.core.paginator import Paginator
from django.db.models import Count, Q

from ..models import Property, TelegramDelivery, TelegramFeed, TelegramPublication, TelegramSubscriber
from .telegram_public_service import VISIBLE_STATUSES, feed_link, public_bot_ready


SKIPPED_ERRORS = ("Подписка остановлена", "Публикация больше не актуальна")


def feed_overview(*, owner=None, agency=None, page_number=None):
    """Return only the listings and delivery state of the requested workspace."""
    if (owner is None) == (agency is None):
        raise ValueError("Укажите личное пространство или команду")

    scope = {"owner": owner, "agency__isnull": True} if owner else {"agency": agency}
    feed_scope = {"owner": owner} if owner else {"agency": agency}
    ready = public_bot_ready()
    if ready:
        feed, _ = TelegramFeed.objects.get_or_create(**feed_scope)
    else:
        feed = TelegramFeed.objects.filter(**feed_scope).first()

    listings = Property.objects.filter(
        telegram_published=True, status__in=VISIBLE_STATUSES, is_demo=False, **scope,
    ).order_by("-updated_at", "-pk")
    page = Paginator(listings, 10).get_page(page_number)
    objects = list(page.object_list)
    publications = {}
    if feed and objects:
        for publication in TelegramPublication.objects.filter(
            feed=feed, property_id__in=[item.pk for item in objects],
        ).order_by("-created_at", "-pk"):
            publications.setdefault(publication.property_id, publication)

    counts = {}
    if publications:
        for row in TelegramDelivery.objects.filter(publication_id__in=[item.pk for item in publications.values()]).values(
            "publication_id"
        ).annotate(
            total=Count("pk"),
            sent=Count("pk", filter=Q(sent_at__isnull=False)),
            pending=Count("pk", filter=Q(sent_at__isnull=True, attempts__lt=5)),
            failed=Count("pk", filter=Q(sent_at__isnull=True, attempts__gte=5) & ~Q(last_error__in=SKIPPED_ERRORS)),
        ):
            counts[row["publication_id"]] = row

    rows = []
    for item in objects:
        publication = publications.get(item.pk)
        state = counts.get(publication.pk, {}) if publication else {}
        sent, pending, failed = (state.get(name, 0) for name in ("sent", "pending", "failed"))
        if not publication:
            label, tone, detail = "Без рассылки", "muted", "Объект ещё не отправлялся подписчикам этой ленты."
        elif failed:
            label, tone, detail = "Есть ошибки", "error", f"Отправлено: {sent} · ожидает: {pending} · ошибок: {failed}"
        elif pending:
            label, tone, detail = "В очереди", "pending", f"Отправлено: {sent} · ожидает: {pending}"
        elif sent:
            label, tone, detail = "Отправлено", "sent", f"Получателей: {sent}"
        elif state.get("total", 0):
            label, tone, detail = "Отменено", "muted", "Отправка отменена после отписки или изменения объекта."
        else:
            label, tone, detail = "Без получателей", "muted", "На момент публикации активных подписчиков не было."
        rows.append({"property": item, "label": label, "tone": tone, "detail": detail})

    return {
        "ready": ready,
        "url": feed_link(feed) if ready and feed else None,
        "subscriber_count": TelegramSubscriber.objects.filter(feed=feed, active=True).count() if feed else 0,
        "page": page,
        "rows": rows,
    }
