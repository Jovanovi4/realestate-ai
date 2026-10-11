from datetime import timedelta
from io import BytesIO

from django.utils import timezone
from django.utils.html import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from properties.models import ClientReminder, Lead, Showing

from .presentation_service import PresentationService


def owner_report_data(link, *, days=30):
    """A factual, buyer-anonymous snapshot for one owner's property."""
    now = timezone.now()
    since = now - timedelta(days=days)
    property = link.property
    leads = Lead.objects.filter(property=property, created_at__gte=since, created_at__lte=now)
    showings = list(Showing.objects.filter(
        deal__property=property, starts_at__gte=since, starts_at__lte=now,
    ).order_by("-starts_at", "-pk"))
    feedback = {
        task.owner_showing_id: task for task in ClientReminder.objects.filter(
            owner_showing_id__in=[showing.pk for showing in showings], client=link.client,
        )
    }
    timeline = [{
        "starts_at": showing.starts_at,
        "status": showing.get_status_display(),
        "reported_at": feedback[showing.pk].completed_at if showing.pk in feedback and feedback[showing.pk].is_done else None,
    } for showing in showings]
    return {
        "owner": link.client, "property": property, "link": link,
        "days": days, "since": since, "until": now,
        "lead_count": leads.count(),
        "contacted_lead_count": leads.filter(first_contacted_at__isnull=False).count(),
        "showing_count": len(showings),
        "completed_showing_count": sum(showing.status == "completed" for showing in showings),
        "reported_showing_count": sum(bool(item["reported_at"]) for item in timeline),
        "showings": timeline,
    }


def owner_report_pdf(report):
    font = PresentationService.register_font()
    ink = colors.HexColor("#172033")
    muted = colors.HexColor("#637083")
    surface = colors.HexColor("#F7F8FA")
    styles = getSampleStyleSheet()
    title = ParagraphStyle("OwnerReportTitle", parent=styles["Title"], fontName=font,
                           fontSize=23, leading=28, textColor=ink, spaceAfter=8)
    heading = ParagraphStyle("OwnerReportHeading", parent=styles["Heading2"], fontName=font,
                             fontSize=13, leading=18, textColor=ink, spaceBefore=17, spaceAfter=8)
    body = ParagraphStyle("OwnerReportBody", parent=styles["Normal"], fontName=font,
                          fontSize=10, leading=15, textColor=ink)
    small = ParagraphStyle("OwnerReportSmall", parent=body, fontSize=9, leading=13, textColor=muted)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.8 * cm, leftMargin=1.8 * cm,
                            topMargin=1.7 * cm, bottomMargin=1.7 * cm,
                            title="Отчёт для собственника", author="REAICRM")
    p = report["property"]
    link = report["link"]
    local = lambda value: timezone.localtime(value).strftime("%d.%m.%Y")
    story = [
        Paragraph("Отчёт для собственника", title),
        Paragraph(escape(f"{report['owner'].name} · {p.title}"), body),
        Paragraph(escape(f"Период: {local(report['since'])} — {local(report['until'])}"), small),
        Spacer(1, .45 * cm),
    ]
    facts = [
        ("Объект", p.title), ("Адрес", p.address or "Не указан"),
        ("Статус", p.get_status_display()),
        ("Публикация лендинга", "Опубликован" if p.landing_published else "Не опубликован"),
    ]
    if p.price is not None:
        facts.append(("Текущая цена", f"{p.price:,.0f} {p.currency}".replace(",", " ") + (" / мес." if p.deal_type == "rent" else "")))
    if link.agreement_expires_on:
        facts.append(("Договорённость до", link.agreement_expires_on.strftime("%d.%m.%Y")))
    table = Table([[Paragraph(escape(label), small), Paragraph(escape(str(value)), body)] for label, value in facts],
                  colWidths=[5 * cm, 11 * cm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), surface),
        ("LINEBELOW", (0, 0), (-1, -2), .35, colors.HexColor("#E5E9EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Paragraph("Активность за период", heading)])
    metrics = [
        ("Обращений", report["lead_count"]),
        ("Первый контакт записан", report["contacted_lead_count"]),
        ("Показов", report["showing_count"]),
        ("Проведено", report["completed_showing_count"]),
        ("Обратная связь передана", report["reported_showing_count"]),
    ]
    story.append(Paragraph(escape(" · ".join(f"{name}: {value}" for name, value in metrics)), body))
    story.append(Paragraph("Показы", heading))
    if report["showings"]:
        rows = [[Paragraph("Дата", small), Paragraph("Состояние", small), Paragraph("Собственник информирован", small)]]
        for item in report["showings"]:
            rows.append([
                Paragraph(escape(local(item["starts_at"])), body),
                Paragraph(escape(item["status"]), body),
                Paragraph(escape(local(item["reported_at"]) if item["reported_at"] else "Пока нет"), body),
            ])
        activity = Table(rows, colWidths=[3.4 * cm, 4.5 * cm, 8.1 * cm], repeatRows=1, hAlign="LEFT")
        activity.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), surface),
            ("LINEBELOW", (0, 0), (-1, -2), .35, colors.HexColor("#E5E9EF")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(activity)
    else:
        story.append(Paragraph("За выбранный период показов не было.", small))
    story.extend([Spacer(1, .5 * cm), Paragraph("Отчёт сформирован из сохранённых данных CRM. Личные данные покупателей не включены.", small)])
    doc.build(story)
    return buffer.getvalue()
