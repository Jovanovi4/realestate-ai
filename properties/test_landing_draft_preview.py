import json
from io import BytesIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from .models import Lead, Property


class LandingDraftPreviewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("preview-owner", password="password")
        self.client.force_login(self.owner)

    @staticmethod
    def editor_data(**overrides):
        return {
            "title": "Новый заголовок",
            "property_type": "apartment",
            "deal_type": "sale",
            "status": "draft",
            "avito_operation": "sell",
            "currency": "RUB",
            "landing_template": "premium",
            "landing_accent": "emerald",
            "landing_button_style": "strict",
            "landing_hero_layout": "background",
            "landing_gallery_style": "grid",
            "landing_title": "Заголовок в предпросмотре",
            "landing_published": "on",
            **overrides,
        }

    def test_preview_uses_unsaved_values_without_publishing_or_creating_leads(self):
        property = Property.objects.create(owner=self.owner, title="Исходный заголовок")

        response = self.client.post(
            reverse("property_landing_draft_preview", args=[property.pk]),
            self.editor_data(landing_enabled_blocks=json.dumps({
                key: key != "gallery" for key, _ in Property.LANDING_BLOCKS
            })),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Заголовок в предпросмотре")
        self.assertContains(response, 'class="landing-accent-emerald landing-buttons-strict landing-hero-background"')
        self.assertContains(response, "Cormorant Garamond")
        self.assertFalse(response.context["landing_enabled"]["gallery"])
        self.assertContains(response, "Форма заявки здесь не отправляется")
        self.assertEqual(response["Cache-Control"], "no-store")
        property.refresh_from_db()
        self.assertEqual(property.title, "Исходный заголовок")
        self.assertEqual(property.landing_template, "classic")
        self.assertFalse(property.landing_published)
        self.assertFalse(Lead.objects.exists())

    def test_new_object_can_be_previewed_without_creating_it(self):
        response = self.client.post(reverse("create_landing_draft_preview"), self.editor_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Заголовок в предпросмотре")
        self.assertFalse(Property.objects.exists())

    def test_preview_shows_unsaved_alternative_block_without_accepting_requests(self):
        property = Property.objects.create(owner=self.owner, title="Объект")
        enabled = {key: key == "alternative_requests" for key, _ in Property.LANDING_BLOCKS}
        response = self.client.post(
            reverse("property_landing_draft_preview", args=[property.pk]),
            self.editor_data(landing_enabled_blocks=json.dumps(enabled)),
        )
        self.assertContains(response, "Подобрать другую недвижимость")
        self.assertContains(response, "В предпросмотре форма не отправляется")
        self.assertNotContains(response, 'name="intake_kind"')
        property.refresh_from_db()
        self.assertFalse(property.landing_enabled_blocks["alternative_requests"])
        self.assertFalse(Lead.objects.exists())

    def test_uploaded_logo_appears_in_preview_without_being_stored(self):
        property = Property.objects.create(owner=self.owner, title="Объект")
        pixels = BytesIO()
        Image.new("RGB", (24, 24), "#334155").save(pixels, format="PNG")
        logo = SimpleUploadedFile("logo.png", pixels.getvalue(), content_type="image/png")

        response = self.client.post(
            reverse("property_landing_draft_preview", args=[property.pk]),
            self.editor_data(landing_logo=logo),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'src="data:image/png;base64,')
        property.refresh_from_db()
        self.assertFalse(property.landing_logo)

    def test_preview_is_limited_to_the_property_workspace(self):
        other = User.objects.create_user("other-owner", password="password")
        property = Property.objects.create(owner=other, title="Чужой объект")

        response = self.client.post(
            reverse("property_landing_draft_preview", args=[property.pk]),
            self.editor_data(),
        )

        self.assertEqual(response.status_code, 404)

    def test_preview_url_does_not_serve_a_saved_or_public_page_on_get(self):
        property = Property.objects.create(owner=self.owner, title="Объект")

        response = self.client.get(reverse("property_landing_draft_preview", args=[property.pk]))

        self.assertEqual(response.status_code, 405)

    def test_invalid_form_reports_errors_without_saving(self):
        property = Property.objects.create(owner=self.owner, title="Исходный заголовок")

        response = self.client.post(
            reverse("property_landing_draft_preview", args=[property.pk]),
            self.editor_data(landing_template="unknown"),
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Шаблон лендинга", status_code=422)
        property.refresh_from_db()
        self.assertEqual(property.title, "Исходный заголовок")
