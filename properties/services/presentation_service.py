from io import BytesIO
from pathlib import Path
import os

from django.utils.html import escape
from PIL import Image as PilImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


class PresentationError(Exception):
    pass


class PresentationService:
    FONT_NAME = "PresentationSans"
    FONT_CANDIDATES = (
        os.environ.get("PRESENTATION_FONT_PATH", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    )

    @classmethod
    def register_font(cls):
        if cls.FONT_NAME in pdfmetrics.getRegisteredFontNames():
            return cls.FONT_NAME
        for candidate in cls.FONT_CANDIDATES:
            if candidate and Path(candidate).is_file():
                pdfmetrics.registerFont(TTFont(cls.FONT_NAME, candidate))
                return cls.FONT_NAME
        raise PresentationError("Не найден шрифт для PDF. Укажите PRESENTATION_FONT_PATH на сервере.")

    @staticmethod
    def image_flowable(property_image, max_width, max_height):
        try:
            property_image.image.open("rb")
            content = property_image.image.read()
            property_image.image.close()
            with PilImage.open(BytesIO(content)) as image:
                image.thumbnail((1600, 1000), PilImage.Resampling.LANCZOS)
                if image.mode in {"RGBA", "LA"}:
                    background = PilImage.new("RGB", image.size, "white")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                width, height = image.size
                source = BytesIO()
                image.save(source, format="JPEG", quality=82, optimize=True)
            ratio = min(max_width / width, max_height / height)
            source.seek(0)
            return Image(source, width=width * ratio, height=height * ratio)
        except (OSError, ValueError):
            return None

    @classmethod
    def build_pdf(cls, property, profile, *, include_facts=True, include_description=True, include_benefits=True, include_contacts=True, photo_limit=6, template="classic"):
        font = cls.register_font()
        is_premium = template == "premium"
        ink = colors.HexColor("#171512" if is_premium else "#172033")
        muted = colors.HexColor("#787168" if is_premium else "#637083")
        accent = colors.HexColor("#b08a4c" if is_premium else "#07866e")
        surface = colors.HexColor("#f5f1ea" if is_premium else "#F7F8FA")
        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer, pagesize=A4, rightMargin=1.7 * cm, leftMargin=1.7 * cm,
            topMargin=1.5 * cm, bottomMargin=1.5 * cm,
            title=property.title,
            author=(profile.display_name if profile and profile.display_name else "Rieltor AI"),
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("PresentationTitle", parent=styles["Title"], fontName=font, fontSize=26 if is_premium else 24, leading=31 if is_premium else 29, textColor=ink, spaceAfter=8)
        subtitle_style = ParagraphStyle("PresentationSubtitle", parent=styles["Normal"], fontName=font, fontSize=10, leading=15, textColor=muted)
        heading_style = ParagraphStyle("PresentationHeading", parent=styles["Heading2"], fontName=font, fontSize=16 if is_premium else 15, leading=20 if is_premium else 19, textColor=ink, spaceBefore=18 if is_premium else 16, spaceAfter=8)
        body_style = ParagraphStyle("PresentationBody", parent=styles["BodyText"], fontName=font, fontSize=10, leading=15, textColor=colors.HexColor("#39352f" if is_premium else "#334155"))
        accent_style = ParagraphStyle("PresentationAccent", parent=styles["Normal"], fontName=font, fontSize=18, leading=22, textColor=accent, spaceAfter=8)
        contact_style = ParagraphStyle("PresentationContact", parent=styles["Normal"], fontName=font, fontSize=9, leading=13, alignment=TA_CENTER, textColor=muted)

        story = [Paragraph(f"{escape(property.get_deal_type_display())} - {escape(property.get_property_type_display())}", subtitle_style), Paragraph(escape(property.marketing_headline or property.title), title_style)]
        if property.price:
            suffix = " / месяц" if property.deal_type == "rent" else ""
            currency = {"RUB": "руб.", "USD": "USD", "EUR": "EUR"}.get(property.currency, property.currency)
            story.append(Paragraph(f"{currency} {property.price}{suffix}", accent_style))
        if property.address:
            story.append(Paragraph(escape(property.address), subtitle_style))
        story.append(Spacer(1, .45 * cm))

        primary = property.primary_image
        if primary:
            image = cls.image_flowable(primary, 17.5 * cm, 10.2 * cm)
            if image:
                story.extend([image, Spacer(1, .35 * cm)])

        facts = []
        for label, value in (("Площадь", f"{property.area} м²" if property.area else None), ("Комнаты", property.rooms), ("Этаж", f"{property.floor} / {property.floors_total}" if property.floor and property.floors_total else property.floor), ("Участок", f"{property.land_area} м²" if property.land_area else None), ("Состояние", property.get_condition_display() if property.condition else None)):
            if value:
                facts.append([Paragraph(label, subtitle_style), Paragraph(str(value), body_style)])
        if property.deal_type == "rent":
            for label, value in (("Мебель", "Есть" if property.furnished else None), ("Залог", str(property.rent_deposit) if property.rent_deposit else None), ("Срок", f"от {property.min_lease_months} мес." if property.min_lease_months else None)):
                if value:
                    facts.append([Paragraph(label, subtitle_style), Paragraph(value, body_style)])
        if facts and include_facts:
            table = Table(facts, colWidths=[4.1 * cm, 4.4 * cm], hAlign="LEFT")
            table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), surface), ("BOX", (0, 0), (-1, -1), .35, accent if is_premium else colors.HexColor("#E5E9EF")), ("INNERGRID", (0, 0), (-1, -1), .35, colors.HexColor("#DED6CA" if is_premium else "#E5E9EF")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
            story.extend([Paragraph("Характеристики", heading_style), table])

        description = property.description or property.short_description
        if description and include_description:
            story.extend([Paragraph("Об объекте", heading_style), Paragraph(escape(description).replace("\n", "<br/>"), body_style)])

        if include_benefits and property.landing_benefits:
            benefits = [benefit for benefit in property.landing_benefits if benefit.get("title")]
            if benefits:
                benefit_text = "<br/>".join(
                    f"<b>{escape(benefit['title'])}</b>{': ' + escape(benefit.get('description', '')) if benefit.get('description') else ''}"
                    for benefit in benefits
                )
                story.extend([Paragraph("Почему выбирают нас", heading_style), Paragraph(benefit_text, body_style)])

        gallery = []
        for property_image in property.images.all()[:photo_limit]:
            image = cls.image_flowable(property_image, 8.45 * cm, 5.6 * cm)
            if image:
                gallery.append(image)
        if len(gallery) > 1:
            rows = [gallery[index:index + 2] for index in range(0, len(gallery), 2)]
            if len(rows[-1]) == 1:
                rows[-1].append("")
            gallery_table = Table(rows, colWidths=[8.6 * cm, 8.6 * cm], hAlign="LEFT")
            gallery_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            story.extend([Paragraph("Фотографии", heading_style), gallery_table])

        if profile and include_contacts:
            name = profile.display_name or property.owner.get_full_name() or property.owner.username
            contacts = " · ".join(filter(None, [profile.phone, f"Telegram: @{profile.telegram_username}" if profile.telegram_username else "", profile.email]))
            story.extend([Spacer(1, .5 * cm), Paragraph(f"Ваш риелтор: {escape(name)}", contact_style), Paragraph(escape(contacts), contact_style) if contacts else Spacer(1, .01 * cm)])

        def decorate_page(canvas, doc):
            canvas.saveState()
            if is_premium:
                canvas.setFillColor(colors.HexColor("#171512"))
                canvas.rect(0, A4[1] - .78 * cm, A4[0], .78 * cm, stroke=0, fill=1)
                canvas.setFillColor(accent)
                canvas.setFont(font, 7)
                canvas.drawString(1.7 * cm, A4[1] - .5 * cm, "RIELTOR AI  |  PRIVATE REAL ESTATE")
            canvas.setFillColor(muted)
            canvas.setFont(font, 7)
            canvas.drawRightString(A4[0] - 1.7 * cm, 1 * cm, f"{doc.page}")
            canvas.restoreState()

        document.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
        return buffer.getvalue()
