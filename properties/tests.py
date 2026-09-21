from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .models import AIContent, Property, PropertyImage
from .services.ai_service import AIServiceError


class AccountAndPropertyAccessTests(TestCase):
    @staticmethod
    def image_upload():
        image = Image.new("RGB", (20, 20), color="white")
        buffer = BytesIO()
        image.save(buffer, format="JPEG")
        return SimpleUploadedFile("property.jpg", buffer.getvalue(), content_type="image/jpeg")

    def test_registration_creates_an_authenticated_user(self):
        response = self.client.post(
            reverse("register"),
            {
                "phone": "+7 (999) 123-45-67",
                "password1": "Secure-agent-password-123",
                "password2": "Secure-agent-password-123",
            },
        )
        self.assertRedirects(response, reverse("property_list"))
        self.assertTrue(User.objects.filter(username="+79991234567").exists())

    def test_agent_cannot_open_another_agents_property(self):
        owner = User.objects.create_user("owner", password="password")
        visitor = User.objects.create_user("visitor", password="password")
        property = Property.objects.create(title="Закрытый объект", owner=owner)
        self.client.force_login(visitor)
        response = self.client.get(reverse("property_detail", args=[property.pk]))
        self.assertEqual(response.status_code, 404)

    def test_owner_can_edit_all_property_card_fields(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)
        response = self.client.post(
            reverse("edit_property", args=[property.pk]),
            {
                "title": "Дом у моря",
                "property_type": "house",
                "deal_type": "sale",
                "status": "published",
                "avito_operation": "sell",
                "price": "450000",
                "currency": "EUR",
                "address": "Лиссабон",
                "area": "180",
                "rooms": "5",
                "bathrooms": "3",
                "floor": "1",
                "floors_total": "2",
                "land_area": "600",
                "year_built": "2022",
                "condition": "excellent",
                "amenities": "Бассейн, парковка",
                "description": "Готовое описание",
                "landing_template": "classic",
                "landing_accent": "emerald",
                "landing_button_style": "rounded",
                "landing_hero_layout": "background",
                "landing_gallery_style": "grid",
                "landing_published": "on",
            },
        )
        self.assertRedirects(response, reverse("property_detail", args=[property.pk]))
        property.refresh_from_db()
        self.assertEqual(property.status, "published")
        self.assertEqual(property.land_area, 600)
        self.assertEqual(property.landing_accent, "emerald")
        self.assertEqual(property.landing_gallery_style, "grid")

    def test_property_accepts_missing_landing_settings_and_rejects_invalid_ones(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        valid_response = self.client.post(
            reverse("edit_property", args=[property.pk]),
            {
                "title": "Квартира у парка",
                "property_type": "apartment",
                "deal_type": "sale",
                "status": "draft",
                "avito_operation": "sell",
                "currency": "RUB",
                "landing_template": "classic",
            },
        )
        self.assertRedirects(valid_response, reverse("property_detail", args=[property.pk]))

        invalid_response = self.client.post(
            reverse("edit_property", args=[property.pk]),
            {
                "title": "Квартира у парка",
                "property_type": "apartment",
                "deal_type": "sale",
                "status": "draft",
                "avito_operation": "sell",
                "currency": "RUB",
                "landing_template": "classic",
                "landing_block_order": "{}",
                "landing_enabled_blocks": "[]",
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertIn("landing_block_order", invalid_response.context["form"].errors)
        self.assertIn("landing_enabled_blocks", invalid_response.context["form"].errors)

    def test_published_landing_accepts_a_lead(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(
            title="Публичный объект", owner=owner, landing_published=True
        )
        landing_url = reverse("public_landing", args=[property.landing_slug])
        self.assertEqual(self.client.get(landing_url).status_code, 200)
        response = self.client.post(
            landing_url,
            {"contact_purpose": "viewing", "name": "Мария", "phone": "+351900000000", "message": "Хочу посмотреть"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(property.leads.count(), 1)
        self.assertEqual(property.leads.get().contact_purpose, "viewing")

    def test_all_landing_templates_render(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)
        landing_url = reverse("public_landing", args=[property.landing_slug])

        for template, _label in Property.LANDING_TEMPLATES:
            property.landing_template = template
            property.save(update_fields=["landing_template", "updated_at"])
            response = self.client.get(landing_url)
            self.assertEqual(response.status_code, 200, template)
            self.assertContains(response, "Записаться на просмотр")

    def test_landing_visual_settings_render_the_selected_variants(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(
            title="Публичный объект",
            owner=owner,
            landing_published=True,
            landing_accent="emerald",
            landing_button_style="strict",
            landing_hero_layout="background",
            landing_gallery_style="grid",
        )
        PropertyImage.objects.create(property=property, image=self.image_upload(), is_primary=True)
        PropertyImage.objects.create(property=property, image=self.image_upload(), order=1)

        response = self.client.get(reverse("public_landing", args=[property.landing_slug]))

        self.assertContains(response, "landing-accent-emerald")
        self.assertContains(response, "landing-buttons-strict")
        self.assertContains(response, "landing-hero-background")
        self.assertContains(response, 'class="gallery-grid"')

    def test_owner_can_download_a_property_presentation_pdf(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Дом у парка", owner=owner, price="4500000", area=120)
        self.client.force_login(owner)

        response = self.client.get(
            reverse("property_presentation_pdf", args=[property.pk]),
            {"facts": "0", "description": "0", "benefits": "0", "contacts": "0", "photos": "0", "template": "premium"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_owner_can_download_an_editable_property_presentation_docx(self):
        owner = User.objects.create_user("agent-docx", password="password")
        property = Property.objects.create(title="Квартира у парка", owner=owner, price="4200000")
        self.client.force_login(owner)

        response = self.client.get(reverse("property_presentation_docx", args=[property.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertTrue(response.content.startswith(b"PK"))

    def test_owner_can_upload_property_image(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.post(
            reverse("upload_images", args=[property.pk]),
            {"images": self.image_upload()},
        )

        self.assertRedirects(response, reverse("property_detail", args=[property.pk]))
        self.assertEqual(property.images.count(), 1)
        self.assertTrue(property.images.get().image.name.endswith(".jpg"))

    def test_property_readiness_lists_all_seven_requirements(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("property_detail", args=[property.pk]))

        self.assertEqual(response.context["readiness_total"], 7)
        self.assertEqual(response.context["readiness_completed"], 0)
        self.assertContains(response, "Назначьте главную фотографию")
        self.assertContains(response, "Укажите контакты риелтора")

    def test_legacy_ai_history_routes_redirect_to_inline_text_editor(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        ai_content = AIContent.objects.create(
            property=property, content_type="headline", tone="business", title=property.title, content="Текст"
        )
        self.client.force_login(owner)
        editor_url = f"{reverse('edit_property', args=[property.pk])}#texts-pane"

        self.assertRedirects(self.client.get(reverse("ai_assistant", args=[property.pk])), editor_url)
        self.assertRedirects(self.client.get(reverse("ai_content_edit", args=[ai_content.pk])), editor_url)
        self.assertRedirects(self.client.post(reverse("ai_content_delete", args=[ai_content.pk])), editor_url)
        self.assertTrue(AIContent.objects.filter(pk=ai_content.pk).exists())

    @patch("properties.ai_views.AIService.generate_inline_content")
    def test_inline_ai_returns_text_without_overwriting_property(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner, description="Ручной текст")
        generate_inline_content.return_value = (
            "full_description",
            {"content": "Текст от ИИ", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1},
        )
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "business", "mode": "generate"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "Текст от ИИ")
        property.refresh_from_db()
        self.assertEqual(property.description, "Ручной текст")

    def test_agent_cannot_generate_inline_text_for_another_property(self):
        owner = User.objects.create_user("owner", password="password")
        visitor = User.objects.create_user("visitor", password="password")
        property = Property.objects.create(title="Закрытый объект", owner=owner)
        self.client.force_login(visitor)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "business", "mode": "generate"},
        )

        self.assertEqual(response.status_code, 404)

    @patch("properties.ai_views.AIService.generate_bundle")
    def test_ai_bundle_returns_preview_without_overwriting_property(self, generate_bundle):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner, short_description="Ручной текст")
        base_result = {"prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1}
        generate_bundle.return_value = [
            {**base_result, "content_type": "headline", "content": "Заголовок"},
            {**base_result, "content_type": "short_description", "content": "Короткий текст"},
            {**base_result, "content_type": "full_description", "content": "Полный текст"},
        ]
        self.client.force_login(owner)

        response = self.client.post(reverse("ai_bundle_generate", args=[property.pk]), {"tone": "business"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["contents"]), 3)
        property.refresh_from_db()
        self.assertEqual(property.short_description, "Ручной текст")

    @patch("properties.ai_views.AIService.generate_bundle")
    def test_ai_landing_bundle_uses_landing_scenario(self, generate_bundle):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        generate_bundle.return_value = [{
            "content_type": "landing_headline", "content": "Заголовок лендинга", "prompt": "test",
            "provider": "ollama", "model": "test", "duration_ms": 1,
        }]
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_bundle_generate", args=[property.pk]), {"tone": "premium", "bundle": "landing"}
        )

        self.assertEqual(response.status_code, 200)
        generate_bundle.assert_called_once_with(property, "premium", "landing")
        self.assertEqual(response.json()["contents"][0]["target"], "landing_title")

    @patch("properties.ai_views.AIService.generate_inline_content")
    def test_all_inline_ai_text_targets_are_available(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        generate_inline_content.return_value = (
            "benefits",
            {"content": "Текст от ИИ", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1},
        )
        self.client.force_login(owner)
        targets = [
            "marketing_headline", "short_description", "description", "landing_title", "landing_subtitle",
            "landing_about_title", "landing_contact_title", "landing_trust_about", "seo_title", "seo_description",
            "landing_benefit_1_title", "landing_benefit_1_description",
        ]

        for target in targets:
            response = self.client.post(
                reverse("ai_inline_generate", args=[property.pk]),
                {"target": target, "tone": "emotional", "mode": "generate"},
            )
            self.assertEqual(response.status_code, 200, target)

    @patch("properties.ai_views.AIService.generate_inline_content")
    def test_inline_ai_accepts_all_tones(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        generate_inline_content.return_value = (
            "full_description",
            {"content": "Текст от ИИ", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1},
        )
        self.client.force_login(owner)

        for tone in ("business", "premium", "concise", "emotional"):
            response = self.client.post(
                reverse("ai_inline_generate", args=[property.pk]),
                {"target": "description", "tone": tone, "mode": "generate"},
            )
            self.assertEqual(response.status_code, 200, tone)

    @patch("properties.ai_views.AIService.improve_text")
    def test_inline_ai_improves_manual_text_without_saving_property(self, improve_text):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner, description="Ручной текст")
        improve_text.return_value = {"content": "Улучшенный текст", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1}
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "premium", "mode": "improve", "improvement": "premium", "source_text": "Ручной текст"},
        )

        self.assertEqual(response.status_code, 200)
        improve_text.assert_called_once_with(property, "full_description", "premium", "premium", "Ручной текст")
        property.refresh_from_db()
        self.assertEqual(property.description, "Ручной текст")

    @patch("properties.ai_views.AIService.generate_inline_content", side_effect=AIServiceError("Ollama недоступна"))
    def test_inline_ai_returns_provider_error(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "business", "mode": "generate"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Ollama недоступна")

    @override_settings(AI_ENABLED=False)
    def test_ai_pages_are_unavailable_when_disabled(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("ai_assistant", args=[property.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(
            self.client.get(reverse("property_detail", args=[property.pk])),
            "ИИ-помощник",
        )
        self.assertNotContains(
            self.client.get(reverse("edit_property", args=[property.pk])),
            "Создать комплект текстов",
        )
        self.assertEqual(
            self.client.post(reverse("ai_inline_generate", args=[property.pk]), {"target": "description"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("ai_bundle_generate", args=[property.pk]), {"tone": "business"}).status_code,
            404,
        )
