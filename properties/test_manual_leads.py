from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientReminder, Deal, Lead, Property
from .services.crm_analytics_service import AnalyticsPeriod, AnalyticsScope, build_report


class ManualLeadTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("manual-lead-owner")
        self.agent = User.objects.create_user("manual-lead-agent")
        self.outsider = User.objects.create_user("manual-lead-outsider")
        self.agency = Agency.objects.create(owner=self.owner, name="Группа")
        AgencyMembership.objects.create(agency=self.agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=self.agency, user=self.agent, role="agent")
        self.private_property = Property.objects.create(owner=self.owner, title="Личный объект")
        self.team_property = Property.objects.create(owner=self.owner, agency=self.agency, title="Общий объект")
        self.client.force_login(self.owner)

    def data(self, **overrides):
        data = {
            "name": "Алексей", "phone": "+79991112233", "message": "Нужна квартира до 8 млн рядом с метро",
            "interest_type": "buy", "source": "manual", "workspace": "personal",
            "assigned_to": str(self.owner.pk), "next_action": "Уточнить район",
        }
        data.update(overrides)
        return data

    def test_personal_request_is_visible_in_leads_tasks_and_analytics(self):
        response = self.client.post(reverse("lead_create"), self.data())
        lead = Lead.objects.get(name="Алексей")
        self.assertRedirects(response, reverse("lead_detail", args=[lead.pk]))
        self.assertIsNone(lead.property_id)
        self.assertEqual((lead.owner_id, lead.agency_id, lead.assigned_to_id), (self.owner.pk, None, self.owner.pk))
        self.assertEqual(lead.client.phone, lead.phone)
        self.assertEqual(lead.message, self.data()["message"])
        self.assertEqual(lead.next_action, "Уточнить район")
        self.assertAlmostEqual((lead.first_response_due_at - lead.created_at).total_seconds(), 1800, delta=0.1)
        self.assertAlmostEqual((lead.next_action_due_at - lead.created_at).total_seconds(), 1800, delta=2)
        task = ClientReminder.objects.get(lead=lead, is_done=False)
        self.assertEqual((task.owner_id, task.agency_id, task.client_id), (self.owner.pk, None, lead.client_id))
        self.assertContains(self.client.get(reverse("lead_list")), "Подбор недвижимости")
        self.assertContains(self.client.get(reverse("workday")), "Подбор недвижимости")
        self.assertContains(self.client.get(reverse("work_items")), "Подбор недвижимости")
        self.assertContains(self.client.get(reverse("lead_detail", args=[lead.pk])), "Привязать предложенный объект")
        self.assertContains(self.client.get(reverse("lead_list"), {"metric": "received", "scope": "personal", "date_from": timezone.localdate().isoformat(), "date_to": timezone.localdate().isoformat()}), "Алексей")
        report = build_report(AnalyticsScope(user_id=self.owner.pk), AnalyticsPeriod.ending_now())
        self.assertEqual(report["during"]["received"], 1)
        self.assertEqual(report["now"]["unanswered"], 1)

        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[lead.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("lead_list")), "Алексей")

    def test_team_request_is_shared_only_with_team_and_assigned_agent(self):
        response = self.client.post(reverse("lead_create"), self.data(
            workspace="team", assigned_to=str(self.agent.pk), phone="+79991112234",
        ))
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get(phone="+79991112234")
        self.assertEqual((lead.agency_id, lead.assigned_to_id), (self.agency.pk, self.agent.pk))
        self.assertEqual(ClientReminder.objects.get(lead=lead).owner_id, self.agent.pk)
        self.assertEqual(build_report(AnalyticsScope(user_id=self.owner.pk), AnalyticsPeriod.ending_now())["during"]["received"], 0)
        self.assertEqual(build_report(AnalyticsScope(user_id=self.owner.pk, agency_id=self.agency.pk), AnalyticsPeriod.ending_now())["during"]["received"], 1)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[lead.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse("workday")), "Подбор недвижимости")
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[lead.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("lead_attach_property", args=[lead.pk]), {"property": self.team_property.pk}).status_code, 404)

    def test_cannot_assign_personal_request_to_teammate_or_attach_cross_scope_property(self):
        response = self.client.post(reverse("lead_create"), self.data(assigned_to=str(self.agent.pk)))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Lead.objects.exists())
        self.client.post(reverse("lead_create"), self.data())
        lead = Lead.objects.get()
        self.client.post(reverse("lead_attach_property", args=[lead.pk]), {"property": self.team_property.pk})
        lead.refresh_from_db()
        self.assertIsNone(lead.property_id)
        self.client.post(reverse("lead_attach_property", args=[lead.pk]), {"property": self.private_property.pk})
        lead.refresh_from_db()
        self.assertEqual(lead.property_id, self.private_property.pk)
        self.assertEqual(lead.client_id, Client.objects.get(phone=lead.phone, agency__isnull=True).pk)

    def test_existing_property_lead_keeps_its_scope_and_contact_flow(self):
        lead = Lead.objects.create(property=self.team_property, name="Мария", phone="+79991112235")
        self.assertEqual((lead.owner_id, lead.agency_id), (self.owner.pk, self.agency.pk))
        self.assertEqual(ClientReminder.objects.get(lead=lead).agency_id, self.agency.pk)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[lead.pk])).status_code, 200)
        self.assertNotContains(self.client.get(reverse("lead_detail", args=[lead.pk])), "Привязать предложенный объект")

    def test_first_contact_and_qualification_work_without_property(self):
        self.client.post(reverse("lead_create"), self.data())
        lead = Lead.objects.get()
        response = self.client.post(reverse("lead_process", args=[lead.pk]), {
            "action": "contacted", "source": "manual", "qualification": "qualified",
            "next_action": "Подобрать три объекта",
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "contact_method": "call", "contact_note": "Клиент уточнил район",
        })
        self.assertEqual(response.status_code, 302)
        lead.refresh_from_db()
        self.assertIsNotNone(lead.first_contacted_at)
        self.assertEqual((lead.qualification, lead.next_action), ("qualified", "Подобрать три объекта"))
        self.assertEqual(lead.action_tasks.get(is_done=False).text, "Подобрать три объекта")

    def test_qualified_search_request_can_be_attached_and_converted_to_a_deal(self):
        self.client.post(reverse("lead_create"), self.data())
        lead = Lead.objects.get()
        self.client.post(reverse("lead_process", args=[lead.pk]), {
            "action": "contacted", "source": "manual", "qualification": "qualified",
            "next_action": "Предложить объект",
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "contact_method": "call", "contact_note": "Подходит квартира",
        })
        before = self.client.get(reverse("lead_detail", args=[lead.pk]))
        self.assertContains(before, "Сначала привяжите объект")
        response = self.client.post(reverse("lead_attach_property", args=[lead.pk]), {"property": self.private_property.pk})
        self.assertRedirects(response, reverse("lead_detail", args=[lead.pk]))
        lead.refresh_from_db()
        self.assertEqual(lead.property_id, self.private_property.pk)
        self.assertEqual(self.client.get(f"{reverse('deal_create')}?lead={lead.pk}").context["form"].initial["property"], self.private_property)
        response = self.client.post(reverse("deal_create"), {
            "client": lead.client_id, "property": self.private_property.pk, "lead": lead.pk,
            "stage": "new", "expected_commission": "100000.00", "outcome_note": "",
        })
        deal = Deal.objects.get(lead=lead)
        self.assertRedirects(response, reverse("deal_detail", args=[deal.pk]))
        lead.refresh_from_db()
        self.assertEqual(lead.outcome, "converted")
        self.assertEqual(lead.outcome_deal_id, deal.pk)

    def test_creating_a_team_transfers_existing_search_requests_and_their_tasks(self):
        founder = User.objects.create_user("new-team-founder")
        self.client.force_login(founder)
        self.assertEqual(self.client.post(reverse("lead_create"), self.data(
            assigned_to=str(founder.pk), phone="+79991112236",
        )).status_code, 302)
        lead = Lead.objects.get(phone="+79991112236")
        task = ClientReminder.objects.get(lead=lead)
        self.assertIsNone(lead.agency_id)
        self.assertEqual(self.client.post(reverse("agency_create"), {"name": "Новая команда"}).status_code, 302)
        lead.refresh_from_db()
        task.refresh_from_db()
        lead.client.refresh_from_db()
        self.assertIsNotNone(lead.agency_id)
        self.assertEqual((lead.agency_id, task.agency_id, lead.client.agency_id),
                         (Agency.objects.get(name="Новая команда").pk,) * 3)


class LeadScopeMigrationTests(TransactionTestCase):
    def test_existing_property_leads_keep_links_and_gain_their_workspace(self):
        previous = [("properties", "0059_agency_closure")]
        target = [("properties", "0060_lead_workspace_and_optional_property")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        user = old.get_model("auth", "User").objects.create(username="old-lead-owner")
        property_model = old.get_model("properties", "Property")
        lead_model = old.get_model("properties", "Lead")
        agency = old.get_model("properties", "Agency").objects.create(owner_id=user.pk, name="Архив")
        personal_property = property_model.objects.create(owner_id=user.pk, title="Личный")
        team_property = property_model.objects.create(owner_id=user.pk, agency_id=agency.pk, title="Командный")
        personal_lead = lead_model.objects.create(property_id=personal_property.pk, name="Личный контакт", phone="+79990001111")
        team_lead = lead_model.objects.create(property_id=team_property.pk, name="Командный контакт", phone="+79990002222")

        MigrationExecutor(connection).migrate(target)
        migrated = MigrationExecutor(connection).loader.project_state(target).apps.get_model("properties", "Lead")
        self.assertEqual((migrated.objects.get(pk=personal_lead.pk).owner_id,
                          migrated.objects.get(pk=personal_lead.pk).agency_id,
                          migrated.objects.get(pk=personal_lead.pk).property_id),
                         (user.pk, None, personal_property.pk))
        self.assertEqual((migrated.objects.get(pk=team_lead.pk).owner_id,
                          migrated.objects.get(pk=team_lead.pk).agency_id,
                          migrated.objects.get(pk=team_lead.pk).property_id),
                         (user.pk, agency.pk, team_property.pk))
