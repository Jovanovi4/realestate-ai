import csv
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Agency, AgencyMembership, CRMChangeEvent, Client, Deal, LandingDailyView,
    Lead, OwnerInquiry, Property, Showing, TelegramDelivery, TelegramFeed,
    TelegramPublication, TelegramSubscriber,
)
from .services.crm_analytics_service import ANALYTICS_TZ, AnalyticsPeriod, AnalyticsScope, resolve_scope
from .services.crm_extended_analytics_service import build_extended_report


class AnalyticsEventTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("event-owner")
        self.property = Property.objects.create(owner=self.user, title="Объект")
        self.client_record = Client.objects.create(owner=self.user, name="Клиент", phone="+79990000001")

    def test_publication_and_stage_changes_are_forward_only_and_atomic(self):
        self.assertIsNone(self.property.first_published_at)
        self.property.status = "published"
        self.property.save(update_fields=["status"])
        self.property.refresh_from_db()
        published_at = self.property.first_published_at
        self.assertIsNotNone(published_at)
        self.property.status = "showing"
        self.property.save(update_fields=["status"])
        self.property.refresh_from_db()
        self.assertEqual(self.property.first_published_at, published_at)
        self.assertEqual(list(CRMChangeEvent.objects.filter(
            resource_type="property", resource_id=self.property.pk, field_name="status",
        ).values_list("new_value", flat=True)), ["showing", "published"])
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.client_record.status = "in_progress"
                self.client_record.save(update_fields=["status"])
                raise RuntimeError("rollback")
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.status, "new")
        self.assertFalse(CRMChangeEvent.objects.filter(resource_type="client", resource_id=self.client_record.pk).exists())

    def test_showing_completion_uses_actual_recording_time(self):
        deal = Deal.objects.create(owner=self.user, client=self.client_record, property=self.property)
        showing = Showing.objects.create(owner=self.user, deal=deal, starts_at=timezone.now() - timedelta(days=2))
        showing.status = "completed"
        showing.outcome_note = "Показ проведён"
        showing.save(update_fields=["status", "outcome_note"])
        showing.refresh_from_db()
        self.assertGreater(showing.completed_at, showing.starts_at)
        completed_at = showing.completed_at
        showing.save(update_fields=["outcome_note"])
        showing.refresh_from_db()
        self.assertEqual(showing.completed_at, completed_at)
        showing.status = "planned"
        showing.save(update_fields=["status"])
        showing.refresh_from_db()
        self.assertIsNone(showing.completed_at)

    def test_assignee_and_deal_stage_transitions_are_recorded(self):
        agent = User.objects.create_user("event-agent")
        agency = Agency.objects.create(name="Команда", owner=self.user)
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        AgencyMembership.objects.create(agency=agency, user=agent, role="agent")
        team_property = Property.objects.create(owner=self.user, agency=agency, title="Командный объект")
        team_client = Client.objects.create(owner=self.user, agency=agency, name="Клиент", phone="+79990000009")
        lead = Lead.objects.create(property=team_property, client=team_client, name="Клиент", phone="+79990000009")
        lead.assigned_to = agent
        lead.save(update_fields=["assigned_to"])
        event = CRMChangeEvent.objects.get(resource_type="lead", resource_id=lead.pk, field_name="assigned_to")
        self.assertEqual((event.old_value, event.new_value, event.agency_id), (str(self.user.pk), str(agent.pk), agency.pk))
        deal = Deal.objects.create(owner=self.user, client=self.client_record, property=self.property)
        deal.stage = "in_progress"
        deal.save(update_fields=["stage"])
        event = CRMChangeEvent.objects.get(resource_type="deal", resource_id=deal.pk, field_name="stage")
        self.assertEqual((event.old_value, event.new_value), ("new", "in_progress"))

    @override_settings(BILLING_ENFORCEMENT_ENABLED=False)
    def test_only_public_non_demo_landing_gets_are_counted(self):
        self.property.landing_published = True
        self.property.save(update_fields=["landing_published"])
        url = reverse("public_landing", args=[self.property.landing_slug])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(LandingDailyView.objects.get(property=self.property).views, 2)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("property_landing_preview", args=[self.property.pk])).status_code, 200)
        self.assertEqual(LandingDailyView.objects.get(property=self.property).views, 2)
        demo = Property.objects.create(owner=self.user, title="Демо", is_demo=True, landing_published=True)
        self.assertEqual(self.client.get(reverse("public_landing", args=[demo.landing_slug])).status_code, 200)
        self.assertFalse(LandingDailyView.objects.filter(property=demo).exists())

    def test_existing_publication_does_not_get_false_historical_date(self):
        Property.objects.filter(pk=self.property.pk).update(status="published")
        self.property.refresh_from_db()
        self.property.title = "Уточнённое название"
        self.property.save(update_fields=["title"])
        self.property.refresh_from_db()
        self.assertIsNone(self.property.first_published_at)


class ExtendedAnalyticsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("report-owner")
        self.other = User.objects.create_user("report-other")
        self.property = Property.objects.create(owner=self.user, title="=опасное название")
        self.client_record = Client.objects.create(owner=self.user, name="Клиент", phone="+79990000002")
        self.now = timezone.now()
        self.period = AnalyticsPeriod.ending_now(now=self.now)
        self.scope = AnalyticsScope(user_id=self.user.pk)

    def test_object_channel_owner_and_comparison_counts(self):
        lead1 = Lead.objects.create(property=self.property, client=self.client_record, name="А", phone="+79990000002", source="landing", qualification="qualified")
        lead2 = Lead.objects.create(property=self.property, client=self.client_record, name="Б", phone="+79990000003", source="landing")
        deal = Deal.objects.create(owner=self.user, client=self.client_record, property=self.property)
        Lead.objects.filter(pk__in=[lead1.pk, lead2.pk]).update(outcome="converted", outcome_deal=deal)
        showing = Showing.objects.create(owner=self.user, deal=deal, starts_at=self.now - timedelta(days=1))
        showing.status = "completed"
        showing.outcome_note = "Проведён"
        showing.save(update_fields=["status", "outcome_note"])
        LandingDailyView.objects.create(property=self.property, day=self.now.astimezone(ANALYTICS_TZ).date(), views=3)
        inquiry = OwnerInquiry.objects.create(owner=self.user, client=self.client_record, assigned_to=self.user, deal_type="sale", address="Адрес")
        OwnerInquiry.objects.filter(pk=inquiry.pk).update(created_at=self.now - timedelta(hours=1))
        report = build_extended_report(self.scope, AnalyticsPeriod.ending_now(now=timezone.now()))
        row = report["properties"][0]
        self.assertEqual((row["views"], row["leads"], row["qualified"], row["converted"], row["showings"]), (3, 2, 1, 2, 1))
        self.assertEqual((report["channels"][0]["received"], report["channels"][0]["converted"]), (2, 2))
        self.assertEqual(report["owners"]["received"], 1)
        self.assertEqual(report["comparison"][0]["current"], 2)
        self.assertEqual(report["comparison"][0]["previous"], 0)

    def test_csv_is_scoped_and_escapes_property_name(self):
        Property.objects.create(owner=self.other, title="Чужой объект")
        self.client.force_login(self.user)
        response = self.client.get(reverse("analytics_export"), {"scope": "personal", "report": "properties"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8-sig")
        self.assertIn("\t=опасное название", content)
        self.assertNotIn("Чужой объект", content)

    def test_outsider_cannot_export_team_objects_by_forging_scope(self):
        agency = Agency.objects.create(name="Команда", owner=self.user)
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        Property.objects.create(owner=self.user, agency=agency, title="Командный секрет")
        self.client.force_login(self.other)
        response = self.client.get(reverse("analytics_export"), {"scope": "team", "report": "properties"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Командный секрет", response.content.decode("utf-8-sig"))

    def test_telegram_delivery_and_bot_response_are_distinct(self):
        feed = TelegramFeed.objects.create(owner=self.user)
        subscriber = TelegramSubscriber.objects.create(feed=feed, chat_id=10001, user_id=10001)
        publication = TelegramPublication.objects.create(property=self.property, feed=feed)
        TelegramDelivery.objects.create(publication=publication, subscriber=subscriber, sent_at=timezone.now())
        Lead.objects.create(property=self.property, name="Отклик", phone="+79990000005", source="telegram_bot")
        report = build_extended_report(self.scope, AnalyticsPeriod.ending_now(now=timezone.now()))
        self.assertEqual(report["telegram_delivered"], 1)
        self.assertEqual(report["telegram_responses"], 1)

    def test_agent_cannot_export_other_members_team_figures(self):
        agency = Agency.objects.create(name="Команда", owner=self.user)
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        agent = User.objects.create_user("report-agent")
        membership = AgencyMembership.objects.create(agency=agency, user=agent, role="agent")
        team_property = Property.objects.create(owner=self.user, agency=agency, title="Командный объект")
        Lead.objects.create(property=team_property, assigned_to=self.user, name="Лид", phone="+79990000004")
        Lead.objects.create(property=team_property, assigned_to=agent, name="Лид агента", phone="+79990000006")
        scope = resolve_scope(agent, requested_scope="team", requested_member=self.user.pk, membership=membership)
        self.assertEqual(scope.member_id, agent.pk)
        report = build_extended_report(scope, AnalyticsPeriod.ending_now(now=timezone.now()))
        self.assertEqual([(row["property"].pk, row["leads"]) for row in report["properties"]], [(team_property.pk, 1)])
        self.client.force_login(agent)
        response = self.client.get(reverse("analytics_export"), {"scope": "team", "member": self.user.pk, "report": "channels"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))
        self.assertIn(["Лендинг", "1", "0", "0"], rows)
        self.assertNotIn("Командный объект", response.content.decode("utf-8-sig"))
