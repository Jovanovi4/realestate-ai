import re

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Client as CRMClient, Lead, Property, RealtorProfile


class ProfileAppearanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("appearance-owner")
        cls.profile = RealtorProfile.objects.create(
            user=cls.user, display_name="Риелтор", email="agent@example.com",
            email_verified_at=timezone.now(), photo="realtors/existing.jpg",
        )
        cls.other = User.objects.create_user("appearance-other")
        cls.other_profile = RealtorProfile.objects.create(user=cls.other)

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("profile_appearance")

    def test_default_is_light_and_profile_offers_all_choices(self):
        self.assertEqual(self.profile.color_scheme, "light")
        response = self.client.get(reverse("edit_profile"))
        self.assertContains(response, 'data-color-scheme="light" data-bs-theme="light"')
        self.assertContains(response, f'action="{self.url}" data-color-scheme-form')
        field = response.context["appearance_form"]["color_scheme"]
        self.assertEqual(field.value(), "light")
        self.assertEqual(list(field.field.choices), list(RealtorProfile.COLOR_SCHEMES))

    def test_preference_updates_only_current_user_and_preserves_contact_data(self):
        before = RealtorProfile.objects.filter(pk=self.profile.pk).values().get()
        for scheme in ("dark", "system", "light"):
            with self.subTest(scheme=scheme):
                response = self.client.post(self.url, {
                    "color_scheme": scheme, "user": self.other.pk,
                    "email": "changed@example.com", "display_name": "Другой",
                }, HTTP_ACCEPT="application/json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"color_scheme": scheme})
                after = RealtorProfile.objects.filter(pk=self.profile.pk).values().get()
                self.assertEqual(after, {**before, "color_scheme": scheme})
        self.other_profile.refresh_from_db()
        self.assertEqual(self.other_profile.color_scheme, "light")

    def test_choice_survives_new_session_and_contact_form_save(self):
        self.client.post(self.url, {"color_scheme": "dark"}, HTTP_ACCEPT="application/json")
        self.client.logout()
        self.client.force_login(self.user)
        response = self.client.get(reverse("edit_profile"))
        self.assertContains(response, 'data-color-scheme="dark" data-bs-theme="dark"')
        self.assertEqual(response.context["appearance_form"]["color_scheme"].value(), "dark")
        response = self.client.post(reverse("edit_profile"), {
            "display_name": "Обновлённое имя", "email": self.profile.email,
            "color_scheme": "light",
        })
        self.assertRedirects(response, reverse("edit_profile"))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.color_scheme, "dark")
        self.assertEqual(self.profile.display_name, "Обновлённое имя")

    def test_preference_is_shared_by_independent_devices_but_not_other_accounts(self):
        second_device = Client()
        second_device.force_login(self.user)
        other_account = Client()
        other_account.force_login(self.other)
        for scheme in ("dark", "system", "light"):
            with self.subTest(scheme=scheme):
                self.client.post(self.url, {"color_scheme": scheme}, HTTP_ACCEPT="application/json")
                page = second_device.get(reverse("lead_list"))
                self.assertContains(page, f'data-color-scheme="{scheme}"')
                self.assertContains(other_account.get(reverse("lead_list")), 'data-color-scheme="light"')

    def test_initial_theme_and_browser_colour_hint_precede_stylesheets(self):
        for scheme, resolved, hint in (("light", "light", "light"), ("dark", "dark", "dark"), ("system", "light", "light dark")):
            with self.subTest(scheme=scheme):
                self.profile.color_scheme = scheme
                self.profile.save(update_fields=["color_scheme"])
                response = self.client.get(reverse("lead_list"))
                self.assertContains(response, f'data-color-scheme="{scheme}" data-bs-theme="{resolved}"')
                self.assertContains(response, f'<meta name="color-scheme" content="{hint}"')
                html = response.content.decode()
                script = re.search(r'<script\b[^>]*src="[^"]*/color-scheme(?:\.[\w]+)?\.js"[^>]*>', html)
                self.assertIsNotNone(script)
                self.assertNotRegex(script.group(), r'\b(?:async|defer|type="module")\b')
                first_style = re.search(r'<link\b[^>]*rel="stylesheet"|<style\b', html)
                self.assertLess(script.start(), first_style.start())
                self.assertLess(html.index('<meta name="color-scheme"'), script.start())

    def test_missing_or_invalid_choice_does_not_change_saved_theme(self):
        self.profile.color_scheme = "dark"
        self.profile.save(update_fields=["color_scheme"])
        for data in ({}, {"color_scheme": "unknown"}, {"color_scheme": "<script>"}):
            response = self.client.post(self.url, data, HTTP_ACCEPT="application/json")
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["message"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.color_scheme, "dark")

    def test_saving_requires_login_and_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        response = self.client.post(self.url, {"color_scheme": "dark"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("login")))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.color_scheme, "light")
        self.assertNotContains(self.client.get(reverse("login")), 'data-color-scheme=')

    def test_csrf_is_required(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        response = browser.post(self.url, {"color_scheme": "dark"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 403)
        browser.get(reverse("edit_profile"))
        response = browser.post(self.url, {"color_scheme": "dark"},
            HTTP_ACCEPT="application/json", HTTP_X_CSRFTOKEN=browser.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 200)

    def test_new_profile_can_save_without_contact_fields(self):
        newcomer = User.objects.create_user("appearance-new")
        self.client.force_login(newcomer)
        self.assertFalse(RealtorProfile.objects.filter(user=newcomer).exists())
        self.assertEqual(self.client.post(self.url, {"color_scheme": "system"},
            HTTP_ACCEPT="application/json").status_code, 200)
        self.assertEqual(RealtorProfile.objects.get(user=newcomer).color_scheme, "system")

    def test_standard_form_submit_redirects_back_to_profile(self):
        response = self.client.post(self.url, {"color_scheme": "dark"})
        self.assertRedirects(response, reverse("edit_profile"))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.color_scheme, "dark")

    def test_dark_lead_workspace_and_htmx_panel_share_the_account_theme(self):
        self.profile.color_scheme = "dark"
        self.profile.save(update_fields=["color_scheme"])
        crm_client = CRMClient.objects.create(owner=self.user, name="Тестовый клиент", phone="+79990000001")
        property = Property.objects.create(owner=self.user, title="Тестовый объект")
        lead = Lead.objects.create(property=property, client=crm_client, name=crm_client.name, phone=crm_client.phone)
        url = reverse("lead_list")
        params = {"lead": lead.pk, "contact": "1"}
        page = self.client.get(url, params)
        self.assertContains(page, 'data-color-scheme="dark" data-bs-theme="dark"')
        self.assertContains(page, 'class="lead-workspace lead-workspace--open"')
        self.assertContains(page, 'id="lead-panel"')
        self.assertContains(page, 'id="id_lead_contact_method"')
        fragment = self.client.get(url, params, HTTP_HX_REQUEST="true")
        self.assertContains(fragment, 'id="lead-panel"')
        self.assertContains(fragment, 'id="id_lead_contact_method"')
        self.assertNotContains(fragment, '<html')
        self.assertNotContains(fragment, 'data-bs-theme=')
