"""Theme boundaries across CRM pages and public landing documents.

These checks render server responses and exercise partial updates, not browser layout.
"""

import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Agency, AgencyMembership, Client, ClientReminder, Deal, Lead, OwnerInquiry,
    Property, PublicIntakeLink, RealtorProfile,
)


@override_settings(AI_ENABLED=True)
class WorkspaceThemeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("theme-workspace")
        cls.profile = RealtorProfile.objects.create(user=cls.user, color_scheme="dark", onboarding_started=True)
        cls.agency = Agency.objects.create(owner=cls.user, name="Тестовая команда")
        AgencyMembership.objects.create(agency=cls.agency, user=cls.user, role="owner")
        cls.property = Property.objects.create(owner=cls.user, agency=cls.agency, title="Тестовый объект", landing_published=True)
        cls.contact = Client.objects.create(owner=cls.user, agency=cls.agency, name="Тестовый клиент", phone="+79990001111", contact_role="both")
        cls.lead = Lead.objects.create(property=cls.property, client=cls.contact, assigned_to=cls.user, name=cls.contact.name, phone=cls.contact.phone)
        cls.deal = Deal.objects.create(owner=cls.user, agency=cls.agency, responsible=cls.user, client=cls.contact, property=cls.property)
        cls.inquiry = OwnerInquiry.objects.create(owner=cls.user, agency=cls.agency, assigned_to=cls.user, client=cls.contact, deal_type="sale", address="Тестовая улица, 1")
        cls.task = ClientReminder.objects.create(owner=cls.user, agency=cls.agency, client=cls.contact, text="Просроченная тестовая задача", due_at=timezone.now() - timedelta(days=1))

    def setUp(self):
        self.client.force_login(self.user)

    def assert_workspace(self, response, mode="dark"):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-color-scheme="{mode}" data-bs-theme="{mode}"')
        self.assertContains(response, 'rieltor-ui.')
        self.assertContains(response, 'id="action-confirmation-modal"')

    def test_daily_pages_full_cards_forms_and_other_sections_use_the_account_theme(self):
        pages = [
            ("workday", []), ("work_items", []), ("task_detail", [self.task.pk]),
            ("lead_list", []), ("lead_detail", [self.lead.pk]),
            ("owner_inquiry_list", []), ("owner_inquiry_detail", [self.inquiry.pk]),
            ("client_list", []), ("client_board", []), ("client_detail", [self.contact.pk]),
            ("deal_board", []), ("deal_detail", [self.deal.pk]), ("showing_create", []),
            ("property_list", []), ("property_detail", [self.property.pk]), ("edit_property", [self.property.pk]),
            ("landing_list", []), ("analytics", []), ("agency_dashboard", []),
            ("agency_workload", []), ("agency_history", []), ("billing:overview", []),
            ("edit_profile", []), ("integration_telegram", []),
        ]
        for mode in ("dark", "light"):
            RealtorProfile.objects.filter(pk=self.profile.pk).update(color_scheme=mode)
            for name, args in pages:
                with self.subTest(mode=mode, page=name):
                    self.assert_workspace(self.client.get(reverse(name, args=args)), mode)

    def test_empty_lists_and_welcome_screen_keep_the_theme(self):
        newcomer = User.objects.create_user("theme-empty")
        RealtorProfile.objects.create(user=newcomer, color_scheme="dark")
        self.client.force_login(newcomer)
        for page in ("property_list", "workday", "lead_list", "client_list", "deal_board", "landing_list", "analytics", "agency_dashboard"):
            with self.subTest(page=page):
                self.assert_workspace(self.client.get(reverse(page)))

    def test_partial_cards_inherit_theme_without_loading_another_page_shell(self):
        panels = (
            ("lead_list", {"lead": self.lead.pk, "contact": 1}, {"HTTP_HX_REQUEST": "true"}, "lead-panel"),
            ("client_list", {"client": self.contact.pk}, {"HTTP_HX_REQUEST": "true"}, "client-panel"),
            ("deal_board", {"deal": self.deal.pk}, {"HTTP_X_DEAL_PANEL": "1"}, "deal-panel"),
        )
        for name, params, headers, panel in panels:
            with self.subTest(panel=panel):
                full = self.client.get(reverse(name), params)
                self.assert_workspace(full)
                fragment = self.client.get(reverse(name), params, **headers)
                self.assertContains(fragment, f'id="{panel}"')
                self.assertNotContains(fragment, '<html')
                self.assertNotContains(fragment, 'data-bs-theme=')

    def test_overdue_task_and_invalid_profile_keep_the_dark_shell(self):
        tasks = self.client.get(reverse("work_items"), {"period": "overdue"})
        self.assert_workspace(tasks)
        self.assertContains(tasks, self.task.text)
        invalid = self.client.post(reverse("edit_profile"), {"email": "not-an-email"})
        self.assert_workspace(invalid)
        self.assertTrue(invalid.context["form"].errors)
        self.assertContains(invalid, 'app-field-error')

    def test_public_landing_and_its_preview_keep_their_own_style_in_both_account_modes(self):
        for template in ("classic", "modern", "premium"):
            self.property.landing_template = template
            self.property.save(update_fields=["landing_template"])
            for route, value in (("public_landing", self.property.landing_slug), ("property_landing_preview", self.property.pk)):
                styles = []
                for mode in ("light", "dark"):
                    RealtorProfile.objects.filter(pk=self.profile.pk).update(color_scheme=mode)
                    with self.subTest(template=template, route=route, mode=mode):
                        response = self.client.get(reverse(route, args=[value]))
                        self.assertEqual(response.status_code, 200)
                        self.assertNotContains(response, 'data-color-scheme=')
                        self.assertNotContains(response, 'color-scheme.js')
                        self.assertNotContains(response, 'rieltor-ui.')
                        styles.append(re.findall(r'<style[^>]*>(.*?)</style>', response.content.decode(), re.S))
                self.assertTrue(styles[0])
                self.assertEqual(styles[0], styles[1])

    def test_public_request_forms_do_not_inherit_a_logged_in_agents_theme(self):
        link, _ = PublicIntakeLink.objects.get_or_create(owner=self.user)
        for route in ("public_search_request", "public_owner_request"):
            response = self.client.get(reverse(route, args=[link.code]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'data-color-scheme=')
            self.assertNotContains(response, 'data-bs-theme=')
            self.assertNotRegex(response.content.decode(), r'<script[^>]+color-scheme')
