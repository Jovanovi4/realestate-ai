from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import PropertyForm
from .landing_colors import contrast, landing_custom_palette
from .models import Agency, AgencyMembership, LandingStylePreset, Property


class LandingCustomColorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("color-owner", password="password")

    def test_extreme_colors_keep_buttons_readable_on_all_themes(self):
        for color in ("#FFFFFF", "#000000", "#FFFF00", "#102030"):
            palette = landing_custom_palette(color)
            self.assertGreaterEqual(contrast(palette["light"], "#FFFFFF"), 4.5)
            self.assertGreaterEqual(contrast(palette["dark"], "#12110F"), 4.5)
            self.assertGreaterEqual(contrast(palette["dark"], "#000000"), 4.5)
        self.assertIsNone(landing_custom_palette("red;--blue:#fff"))

    def test_custom_hex_is_validated_and_rendered_in_each_public_template(self):
        self.client.force_login(self.user)
        data = {
            "title": "Лендинг", "property_type": "apartment", "deal_type": "sale",
            "status": "draft", "avito_operation": "sell", "currency": "RUB",
            "landing_template": "classic", "landing_accent": "custom",
            "landing_custom_color": "#ffffff",
        }
        self.assertFalse(PropertyForm({**data, "landing_custom_color": "#123"}, user=self.user).is_valid())
        self.assertFalse(PropertyForm({**data, "landing_custom_color": "red;--x:1"}, user=self.user).is_valid())
        form = PropertyForm(data, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["landing_custom_color"], "#FFFFFF")

        property = Property.objects.create(
            owner=self.user, title="Лендинг", landing_published=True,
            landing_accent="custom", landing_custom_color="#FFFFFF",
        )
        for template, variable, expected in (
            ("classic", "--accent", "light"),
            ("modern", "--blue", "light"),
            ("premium", "--gold", "dark"),
        ):
            property.landing_template = template
            property.save(update_fields=["landing_template"])
            response = self.client.get(reverse("public_landing", args=[property.landing_slug]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f"{variable}:{landing_custom_palette('#FFFFFF')[expected]}")

        preview = self.client.post(
            reverse("property_landing_draft_preview", args=[property.pk]),
            {**data, "landing_template": "modern", "landing_custom_color": "#000000"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, f"--blue:{landing_custom_palette('#000000')['light']}")
        property.refresh_from_db()
        self.assertEqual(property.landing_custom_color, "#FFFFFF")

        property.landing_custom_color = "red;--blue:#fff"
        property.save(update_fields=["landing_custom_color"])
        response = self.client.get(reverse("public_landing", args=[property.landing_slug]))
        self.assertNotContains(response, "red;--blue")


class LandingStylePresetTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("preset-owner", password="password")
        self.agent = User.objects.create_user("preset-agent", password="password")
        self.outsider = User.objects.create_user("preset-outsider", password="password")
        self.agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=self.agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=self.agency, user=self.agent, role="agent")
        self.shared = Property.objects.create(owner=self.owner, agency=self.agency, title="Общий объект")
        self.private = Property.objects.create(owner=self.owner, title="Личный объект")
        self.url = reverse("landing_style_preset_create")
        self.style = {
            "name": "Фирменный", "landing_template": "premium", "landing_accent": "custom",
            "landing_custom_color": "#c08030", "landing_button_style": "strict",
            "landing_hero_layout": "background", "landing_gallery_style": "grid",
        }

    def test_team_preset_is_shared_for_application_but_only_manager_can_change_it(self):
        self.client.force_login(self.owner)
        created = self.client.post(self.url, {**self.style, "property_id": self.shared.pk})
        self.assertEqual(created.status_code, 201)
        preset = LandingStylePreset.objects.get(pk=created.json()["preset"]["id"])
        self.assertEqual(preset.agency, self.agency)
        self.assertEqual(preset.landing_custom_color, "#C08030")

        self.client.force_login(self.agent)
        response = self.client.get(reverse("edit_property", args=[self.shared.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["landing_style_presets"][0]["id"], preset.pk)
        self.assertFalse(response.context_data["landing_preset_can_manage"])
        self.assertEqual(self.client.post(self.url, {**self.style, "property_id": self.shared.pk}).status_code, 403)
        delete = reverse("landing_style_preset_delete", args=[preset.pk])
        self.assertEqual(self.client.post(delete, {"property_id": self.shared.pk}).status_code, 403)

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self.url, {**self.style, "property_id": self.shared.pk}).status_code, 404)
        self.assertEqual(self.client.post(delete, {"property_id": self.shared.pk}).status_code, 404)
        self.assertEqual(self.client.post(self.url, {**self.style, "property_id": "invalid"}).status_code, 404)

    def test_personal_preset_stays_private_and_deletion_does_not_change_object(self):
        self.client.force_login(self.owner)
        created = self.client.post(self.url, {**self.style, "property_id": self.private.pk})
        self.assertEqual(created.status_code, 201)
        preset = LandingStylePreset.objects.get(pk=created.json()["preset"]["id"])
        self.assertEqual(preset.owner, self.owner)
        self.assertIsNone(preset.agency)
        self.assertEqual(self.client.post(self.url, {**self.style, "property_id": self.private.pk}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {**self.style, "landing_custom_color": "bad", "property_id": self.private.pk}).status_code, 400)

        self.client.force_login(self.agent)
        response = self.client.get(reverse("edit_property", args=[self.shared.pk]))
        self.assertNotIn(preset.pk, [item["id"] for item in response.context_data["landing_style_presets"]])

        self.client.force_login(self.owner)
        delete = reverse("landing_style_preset_delete", args=[preset.pk])
        self.assertEqual(self.client.post(delete, {"property_id": self.private.pk}).status_code, 200)
        self.private.refresh_from_db()
        self.assertEqual(self.private.landing_template, "classic")
        self.assertFalse(LandingStylePreset.objects.filter(pk=preset.pk).exists())
