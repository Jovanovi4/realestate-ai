import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import PropertyForm
from .models import Property, RealtorProfile


class LandingCopyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("landing-copy-owner", password="password")
        self.property = Property.objects.create(
            owner=self.owner,
            title="Квартира у парка",
            landing_title="Квартира у парка — 285 000 €",
            price=Decimal("285000"),
            currency="EUR",
            landing_published=True,
        )

    def test_each_public_template_keeps_price_out_of_heading_and_hides_placeholder_trust(self):
        url = reverse("public_landing", args=[self.property.landing_slug])
        for template, _ in Property.LANDING_TEMPLATES:
            with self.subTest(template=template):
                self.property.landing_template = template
                self.property.save(update_fields=["landing_template"])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", response.content.decode(), re.S)
                self.assertIsNotNone(heading)
                self.assertEqual(heading.group(1), "Квартира у парка")
                self.assertContains(response, '<p class="price mb-1">285 000 €</p>')
                self.assertNotContains(response, "Почему выбирают нас")
                self.assertNotContains(response, 'class="section trust-section"')

    def test_authored_realtor_facts_replace_generic_cards_and_process(self):
        RealtorProfile.objects.create(
            user=self.owner,
            display_name="Анна Л.",
            phone="+351900000000",
            about="Работаю с жилыми объектами в Алваладе.",
            benefits=[{"icon": "✓", "title": "Показы лично", "description": "Провожу просмотр и отвечаю на вопросы об объекте."}],
        )
        self.property.landing_template = "modern"
        self.property.save(update_fields=["landing_template"])

        response = self.client.get(reverse("public_landing", args=[self.property.landing_slug]))

        self.assertContains(response, "О риелторе")
        self.assertContains(response, "Работаю с жилыми объектами в Алваладе.")
        self.assertContains(response, "Показы лично")
        self.assertContains(response, 'href="tel:+351900000000"')
        self.assertNotContains(response, "Знание рынка")
        self.assertNotContains(response, "Как проходит сделка")

    def test_editor_starts_without_generic_benefits_and_preserves_partial_draft(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("edit_property", args=[self.property.pk]))
        self.assertContains(response, "Укажите название объекта без цены")
        self.assertContains(response, "Пустые карточки не публикуются")
        editor = PropertyForm(instance=self.property, user=self.owner)
        self.assertFalse(editor.initial["landing_benefit_1_title"])
        form = PropertyForm(data={
            "title": self.property.title,
            "property_type": "apartment",
            "deal_type": "sale",
            "status": "draft",
            "avito_operation": "sell",
            "currency": "EUR",
            "landing_template": "classic",
            "landing_benefit_1_title": "Показы лично",
            "landing_benefit_1_description": "Провожу просмотр сама.",
            "landing_benefit_2_title": "Черновик",
        }, instance=self.property, user=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual([item["title"] for item in saved.landing_benefits], ["Показы лично", "Черновик"])
        self.assertEqual(saved.landing_benefits[1]["description"], "")

    def test_property_overview_omits_unknown_land_area(self):
        self.client.force_login(self.owner)
        url = reverse("property_detail", args=[self.property.pk])
        response = self.client.get(url)
        facts = response.content.decode().split('<dl class="detail-facts">', 1)[1].split("</dl>", 1)[0]
        self.assertNotIn("Участок", facts)

        self.property.land_area = 120
        self.property.save(update_fields=["land_area"])
        response = self.client.get(url)
        facts = response.content.decode().split('<dl class="detail-facts">', 1)[1].split("</dl>", 1)[0]
        self.assertIn("<dt>Участок</dt><dd>120 м²</dd>", facts)
