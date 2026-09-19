from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Property


class AccountAndPropertyAccessTests(TestCase):
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
                "landing_published": "on",
            },
        )
        self.assertRedirects(response, reverse("property_detail", args=[property.pk]))
        property.refresh_from_db()
        self.assertEqual(property.status, "published")
        self.assertEqual(property.land_area, 600)

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
            {"name": "Мария", "phone": "+351900000000", "message": "Хочу посмотреть"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(property.leads.count(), 1)

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
