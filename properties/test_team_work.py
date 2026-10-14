from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Agency, AgencyMembership, AuditEvent, CRMChangeEvent, Client, ClientReminder,
    Deal, Lead, Property, PropertyOwnerLink, Showing,
)


class TeamWorkTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.owner = User.objects.create_user("transfer-owner")
        self.manager = User.objects.create_user("transfer-manager")
        self.agent = User.objects.create_user("transfer-agent")
        self.outsider = User.objects.create_user("transfer-outsider")
        self.agency = Agency.objects.create(owner=self.owner, name="Команда передачи")
        for user, role in ((self.owner, "owner"), (self.manager, "manager"), (self.agent, "agent")):
            AgencyMembership.objects.create(agency=self.agency, user=user, role=role)
        self.member = AgencyMembership.objects.get(agency=self.agency, user=self.agent)
        self.property = Property.objects.create(owner=self.agent, agency=self.agency, title="Объект команды")
        self.contact = Client.objects.create(owner=self.agent, agency=self.agency, name="Контакт", phone="+79990001010")
        self.lead = Lead.objects.create(
            property=self.property, client=self.contact, assigned_to=self.agent,
            name="Покупатель", phone="+79990001010",
        )
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=self.now - timedelta(minutes=45), next_action_due_at=self.now + timedelta(hours=2),
        )
        self.task = ClientReminder.objects.create(
            owner=self.agent, agency=self.agency, client=self.contact,
            text="Позвонить клиенту", due_at=self.now - timedelta(minutes=5),
        )
        self.unassigned = Lead.objects.create(
            property=self.property, client=self.contact, name="Без назначения", phone="+79990002020",
        )
        Lead.objects.filter(pk=self.unassigned.pk).update(assigned_to=None)

    def test_manager_queue_shows_leads_and_independent_tasks_without_duplicate_lead_task(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("agency_workload"), {"member": self.agent.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({lead.pk for lead in response.context["lead_page"]}, {self.lead.pk})
        self.assertEqual({task.pk for task in response.context["task_page"]}, {self.task.pk})
        self.assertEqual(self.client.get(reverse("task_detail", args=[self.task.pk])).status_code, 200)
        overview = self.client.get(reverse("agency_dashboard"))
        rows = {row["user_id"]: row for row in overview.context["work_members"]}
        self.assertEqual(rows[self.agent.pk]["urgent_work"], 2)
        analytics = self.client.get(reverse("analytics"), {"scope": "team"})
        analytics_rows = {row["user_id"]: row for row in analytics.context["member_rows"]}
        self.assertEqual(rows[self.agent.pk]["urgent_work"], analytics_rows[self.agent.pk]["urgent_work"])
        overdue = self.client.get(reverse("agency_workload"), {"state": "overdue", "member": self.agent.pk})
        self.assertEqual({lead.pk for lead in overdue.context["lead_page"]}, {self.lead.pk})
        self.assertEqual({task.pk for task in overdue.context["task_page"]}, {self.task.pk})

    def test_quick_assignment_updates_lead_task_and_history(self):
        self.client.force_login(self.manager)
        url = reverse("agency_work_assign")
        self.assertEqual(self.client.post(url, {
            "kind": "lead", "record_id": self.unassigned.pk, "assigned_to": self.manager.pk,
            "next": reverse("agency_workload") + "?state=unassigned&kind=lead",
        }).status_code, 302)
        self.unassigned.refresh_from_db()
        self.assertEqual(self.unassigned.assigned_to, self.manager)
        self.assertEqual(ClientReminder.objects.get(lead=self.unassigned, is_done=False).owner, self.manager)
        self.assertTrue(CRMChangeEvent.objects.filter(
            resource_type="lead", resource_id=self.unassigned.pk, field_name="assigned_to",
            old_value="", new_value=str(self.manager.pk),
        ).exists())
        self.assertTrue(AuditEvent.objects.filter(agency=self.agency, actor=self.manager,
                                                  object_pk=str(self.unassigned.pk), action="updated").exists())

        self.assertEqual(self.client.post(url, {
            "kind": "task", "record_id": self.task.pk, "assigned_to": self.owner.pk,
        }).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.owner, self.owner)
        self.assertTrue(CRMChangeEvent.objects.filter(
            resource_type="clientreminder", resource_id=self.task.pk, field_name="owner",
            old_value=str(self.agent.pk), new_value=str(self.owner.pk),
        ).exists())

    def test_assignment_is_limited_to_managers_open_team_work_and_current_members(self):
        url = reverse("agency_work_assign")
        data = {"kind": "task", "record_id": self.task.pk, "assigned_to": self.owner.pk}
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("agency_workload")).status_code, 404)
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("task_detail", args=[self.task.pk])).status_code, 404)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, {**data, "assigned_to": self.outsider.pk}).status_code, 404)
        self.task.is_done = True
        self.task.save(update_fields=["is_done"])
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.task.refresh_from_db()
        self.assertEqual(self.task.owner, self.agent)

    def test_reassigned_owner_feedback_task_can_be_opened_and_completed(self):
        deal = Deal.objects.create(owner=self.agent, agency=self.agency, responsible=self.agent,
                                   client=self.contact, property=self.property)
        showing = Showing.objects.create(owner=self.agent, agency=self.agency, deal=deal,
            starts_at=self.now - timedelta(hours=2), status="completed", outcome_note="Показ состоялся")
        PropertyOwnerLink.objects.create(property=self.property, client=self.contact)
        feedback = ClientReminder.objects.create(
            owner=self.agent, agency=self.agency, client=self.contact, property=self.property,
            owner_showing=showing, text="Сообщить собственнику", due_at=self.now - timedelta(minutes=5),
        )
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(reverse("agency_work_assign"), {
            "kind": "task", "record_id": feedback.pk, "assigned_to": self.manager.pk,
        }).status_code, 302)
        self.assertRedirects(self.client.get(reverse("task_detail", args=[feedback.pk])),
                             reverse("showing_detail", args=[showing.pk]) + "#owner-feedback", fetch_redirect_response=False)
        showing_page = self.client.get(reverse("showing_detail", args=[showing.pk]))
        self.assertContains(showing_page, "Записать сообщение")
        response = self.client.post(reverse("owner_feedback_complete", args=[showing.pk, feedback.pk]), {
            "method": "call", "feedback_note": "Условия показа согласованы",
        })
        self.assertEqual(response.status_code, 302)
        feedback.refresh_from_db()
        self.assertTrue(feedback.is_done)

    def test_removal_previews_load_and_transfers_to_chosen_manager(self):
        deal = Deal.objects.create(owner=self.agent, agency=self.agency, responsible=self.agent,
                                   client=self.contact, property=self.property)
        showing = Showing.objects.create(owner=self.agent, agency=self.agency, deal=deal,
                                         starts_at=self.now + timedelta(days=1))
        self.client.force_login(self.owner)
        url = reverse("agency_member_remove", args=[self.member.pk])
        preview = self.client.get(url)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.context["counts"]["active_leads"], 1)
        self.assertEqual(preview.context["counts"]["open_tasks"], 3)
        self.assertEqual(preview.context["counts"]["active_deals"], 1)
        self.assertEqual(preview.context["counts"]["planned_showings"], 1)
        self.assertEqual(preview.context["counts"]["unassigned_team_leads"], 1)
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertTrue(AgencyMembership.objects.filter(pk=self.member.pk).exists())
        response = self.client.post(url, {
            "replacement": self.manager.pk, "preview_token": preview.context["preview_token"],
            "confirm": "transfer_and_remove",
        })
        self.assertRedirects(response, reverse("agency_dashboard"))
        self.assertFalse(AgencyMembership.objects.filter(pk=self.member.pk).exists())
        for record in (self.property, self.contact, self.lead, self.task, deal, showing):
            record.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, self.manager)
        self.unassigned.refresh_from_db()
        self.assertEqual(self.unassigned.assigned_to, self.manager)
        self.assertEqual(self.task.owner, self.manager)
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).owner, self.manager)
        self.assertEqual((self.property.owner, self.contact.owner, deal.owner, deal.responsible, showing.owner),
                         (self.manager,) * 5)
        self.assertFalse(Lead.objects.filter(property__agency=self.agency, outcome="open", assigned_to=self.agent).exists())
        self.assertFalse(ClientReminder.objects.filter(agency=self.agency, owner=self.agent, is_done=False).exists())
        self.assertTrue(CRMChangeEvent.objects.filter(resource_type="lead", resource_id=self.lead.pk,
                                                      field_name="assigned_to", new_value=str(self.manager.pk)).exists())

    def test_changed_load_requires_new_preview_and_manager_cannot_remove_member(self):
        url = reverse("agency_member_remove", args=[self.member.pk])
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_login(self.owner)
        preview = self.client.get(url)
        ClientReminder.objects.create(owner=self.agent, agency=self.agency, client=self.contact,
                                      text="Новое дело", due_at=self.now + timedelta(days=1))
        response = self.client.post(url, {
            "replacement": self.manager.pk, "preview_token": preview.context["preview_token"],
            "confirm": "transfer_and_remove",
        })
        self.assertEqual(response.status_code, 409)
        self.assertTrue(AgencyMembership.objects.filter(pk=self.member.pk).exists())
        self.assertContains(response, "Нагрузка изменилась", status_code=409)

    def test_removal_rejects_foreign_recipient_and_modified_preview(self):
        self.client.force_login(self.owner)
        url = reverse("agency_member_remove", args=[self.member.pk])
        preview = self.client.get(url)
        data = {"replacement": self.manager.pk, "preview_token": preview.context["preview_token"],
                "confirm": "transfer_and_remove"}
        self.assertEqual(self.client.post(url, {**data, "replacement": self.outsider.pk}).status_code, 404)
        self.assertEqual(self.client.post(url, {**data, "preview_token": "modified"}).status_code, 400)
        self.assertTrue(AgencyMembership.objects.filter(pk=self.member.pk).exists())
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, self.agent)
