import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib import admin
from django.test import RequestFactory
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from properties.models import AIContent, Agency, AgencyMembership, Client, ClientReminder, Property, PropertyImage, RealtorProfile

from .models import ResourceReservation, Subscription, UsageOperation
from .services import AccountService, SubscriptionService
from .tests import seed_test_catalog


@override_settings(BILLING_ENFORCEMENT_ENABLED=True, BILLING_SHADOW_ENABLED=True)
class EnforcedBillingTests(TestCase):
    def setUp(self):
        seed_test_catalog()
        self.user = get_user_model().objects.create_user("enforced-owner")
        self.account = AccountService.ensure_personal(self.user)
        self.client.force_login(self.user)

    def create_form(self, **updates):
        return {"title": "Новый объект", "property_type": "apartment", "deal_type": "sale", "status": "draft",
            "avito_operation": "sell", "currency": "RUB", "landing_template": "classic",
            "request_key": str(uuid.uuid4()), **updates}

    def test_last_demo_property_slot_and_duplicate_request(self):
        Property.objects.create(owner=self.user, title="Первый")
        form = self.create_form()
        first = self.client.post(reverse("create_property"), form)
        retry = self.client.post(reverse("create_property"), form)
        denied = self.client.post(reverse("create_property"), self.create_form(title="Третий"))
        self.assertEqual((first.status_code, retry.status_code, denied.status_code), (302, 302, 403))
        self.assertEqual(Property.objects.filter(owner=self.user).count(), 2)
        self.assertContains(denied, "Лимит исчерпан", status_code=403)
        self.assertEqual(ResourceReservation.objects.filter(account=self.account, status="consumed").count(), 1)

    def test_published_landing_claim_checks_second_quota(self):
        Property.objects.create(owner=self.user, title="Публичный", landing_published=True)
        response = self.client.post(reverse("create_property"), self.create_form(landing_published="on"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Лимит публичных лендингов исчерпан", status_code=403)
        self.assertEqual(Property.objects.filter(owner=self.user).count(), 1)

    def test_both_restore_routes_respect_active_property_limit(self):
        Property.objects.create(owner=self.user, title="Первый")
        Property.objects.create(owner=self.user, title="Второй")
        archived = Property.objects.create(owner=self.user, title="Архив", status="archived")
        first = self.client.post(reverse("restore_property", args=[archived.pk]))
        second = self.client.post(reverse("update_property_status", args=[archived.pk]), {"status": "draft"})
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        archived.refresh_from_db()
        self.assertEqual(archived.status, "archived")

    def test_invitation_requires_team_feature_and_admin_cannot_bypass(self):
        agency = Agency.objects.create(owner=self.user, name="Агентство")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        response = self.client.post(reverse("agency_invite"), {"role": "agent", "request_key": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(agency.invitations.exists())
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_user("staff", is_staff=True, is_superuser=True)
        self.assertFalse(admin.site._registry[Property].has_add_permission(request))
        self.assertFalse(admin.site._registry[Property].has_change_permission(request))
        self.assertFalse(admin.site._registry[Client].has_add_permission(request))
        self.assertFalse(admin.site._registry[Agency].has_add_permission(request))

    def test_ai_denial_happens_before_provider_call(self):
        prop = Property.objects.create(owner=self.user, title="Объект")
        RealtorProfile.objects.create(user=self.user, email="owner@example.test", email_verified_at=timezone.now())
        grant = SubscriptionService.ensure_demo_grant(self.user)
        grant.credits_used = grant.credits_allocated
        grant.save(update_fields=["credits_used"])
        with patch("properties.ai_views.AIService.generate_inline_content") as provider:
            response = self.client.post(reverse("ai_inline_generate", args=[prop.pk]),
                {"target": "marketing_headline"}, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "quota_exceeded")
        provider.assert_not_called()
        self.assertFalse(AIContent.objects.exists())
        self.assertFalse(UsageOperation.objects.filter(ai_action="short_text").exists())

    def test_over_quota_public_landing_denies_get_and_post_without_leaking_billing(self):
        first = Property.objects.create(owner=self.user, title="Первый", landing_published=True)
        Property.objects.create(owner=self.user, title="Второй", landing_published=True)
        url = reverse("public_landing", args=[first.landing_slug])
        self.client.logout()
        for response in (self.client.get(url), self.client.post(url, {})):
            self.assertEqual(response.status_code, 503)
            self.assertContains(response, "Страница временно недоступна", status_code=503)
            self.assertNotContains(response, "тариф", status_code=503)

    def test_demo_xml_download_is_denied(self):
        prop = Property.objects.create(owner=self.user, title="Для экспорта", avito_export=True)
        with patch("properties.views.AvitoExportService.get_exportable", return_value=([prop], [])), \
                patch("properties.views.AvitoExportService.build_xml") as build:
            response = self.client.get(reverse("avito_export"), {"download": "1"})
        self.assertEqual(response.status_code, 302)
        build.assert_not_called()

    def test_suspended_account_cannot_reorder_or_delete_images(self):
        prop = Property.objects.create(owner=self.user, title="Галерея")
        first = PropertyImage.objects.create(property=prop, image="properties/first.jpg", order=0)
        second = PropertyImage.objects.create(property=prop, image="properties/second.jpg", order=1)
        Subscription.objects.create(account=self.account, anchor_at=timezone.now(),
            is_suspended=True, suspension_reason="Тест")
        reorder = self.client.post(reverse("reorder_images", args=[prop.pk]),
            data='{"image_ids": [%d, %d]}' % (second.pk, first.pk), content_type="application/json")
        removal = self.client.post(reverse("delete_property_image", args=[prop.pk, first.pk]),
            HTTP_HX_REQUEST="true")
        self.assertEqual(reorder.status_code, 403)
        self.assertEqual(reorder.json()["code"], "subscription_suspended")
        self.assertContains(removal, "Данные доступны для просмотра", status_code=200)
        self.assertEqual(list(prop.images.values_list("pk", "order").order_by("order")),
            [(first.pk, 0), (second.pk, 1)])

    def test_demo_team_role_change_is_denied(self):
        agency = Agency.objects.create(owner=self.user, name="Агентство")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        teammate = get_user_model().objects.create_user("teammate")
        target = AgencyMembership.objects.create(agency=agency, user=teammate, role="agent")
        response = self.client.post(reverse("agency_member_role", args=[target.pk]), {"role": "manager"})
        target.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(target.role, "agent")

    def test_suspended_account_cannot_create_client_or_task(self):
        Subscription.objects.create(account=self.account, anchor_at=timezone.now(),
            is_suspended=True, suspension_reason="Тест")
        client = self.client.post(reverse("client_create"),
            {"name": "Новый", "phone": "+70000000099", "source": "manual", "status": "new"})
        task = self.client.post(reverse("task_create"), {"text": "Позвонить", "due_at": "2030-01-01T12:00"})
        self.assertEqual((client.status_code, task.status_code), (403, 403))
        self.assertFalse(Client.objects.filter(name="Новый").exists())
        self.assertFalse(ClientReminder.objects.filter(text="Позвонить").exists())

    def test_team_member_cannot_edit_demo_team_but_can_use_personal_space(self):
        agency = Agency.objects.create(owner=self.user, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        teammate = get_user_model().objects.create_user("team-member")
        AgencyMembership.objects.create(agency=agency, user=teammate, role="agent")
        self.client.force_login(teammate)
        denied = self.client.post(reverse("client_create"),
            {"name": "Командный", "phone": "+70000000098", "source": "manual", "status": "new"})
        allowed = self.client.post(reverse("task_create"),
            {"text": "Личная задача", "due_at": "2030-01-01T12:00"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 302)
        self.assertFalse(Client.objects.filter(name="Командный").exists())
        self.assertTrue(ClientReminder.objects.filter(owner=teammate, agency__isnull=True, text="Личная задача").exists())
