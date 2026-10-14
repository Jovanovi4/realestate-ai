from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientReminder, Lead, OwnerInquiry


class OwnerInquiryInLeadQueueTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("queue-owner")
        self.agent = User.objects.create_user("queue-agent")
        self.outsider = User.objects.create_user("queue-outsider")
        self.client.force_login(self.owner)

    def inquiry(self, *, owner=None, agency=None, assignee=None, name="Мария", address="Лесная, 5"):
        owner = owner or self.owner
        contact = Client.objects.create(
            owner=owner, agency=agency, name=name, phone="+79991001010", contact_role="owner",
        )
        due = timezone.now() + timedelta(minutes=30)
        return OwnerInquiry.objects.create(
            owner=owner, agency=agency, client=contact, assigned_to=assignee or owner,
            deal_type="sale", address=address, first_response_due_at=due, next_action_due_at=due,
        )

    def test_owner_request_appears_once_in_all_and_needs_response(self):
        inquiry = self.inquiry()
        response = self.client.get(reverse("lead_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertEqual(response.context["queue_tabs"][0]["count"], 1)
        self.assertEqual(response.context["nav_new_leads_count"], 1)
        self.assertContains(response, "Продать · Лесная, 5")
        self.assertContains(response, reverse("owner_inquiry_detail", args=[inquiry.pk]))
        self.assertEqual(Lead.objects.count(), 0)
        self.assertEqual(ClientReminder.objects.count(), 0)
        self.assertEqual(self.client.get(reverse("lead_list"), {"queue": "needs_response"}).context["page_obj"].paginator.count, 1)
        self.assertEqual(self.client.get(reverse("lead_list"), {"q": "Лесная"}).context["page_obj"].paginator.count, 1)
        self.assertEqual(self.client.get(reverse("lead_list"), {"q": "другой адрес"}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("lead_list"), {"status": "new"}).context["page_obj"].paginator.count, 1)

    def test_owner_and_client_requests_have_separate_selection_even_with_same_id(self):
        inquiry = self.inquiry()
        lead = Lead.objects.create(
            owner=self.owner, name="Покупатель", phone="+79990000000",
            next_action="Позвонить", next_action_due_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(inquiry.pk, lead.pk)

        response = self.client.get(reverse("lead_list"))

        self.assertEqual(response.context["selectable_lead_ids_json"], f'["{lead.pk}"]')
        self.assertEqual(response.context["selectable_owner_ids_json"], f'["{inquiry.pk}"]')
        self.assertEqual(response.context["selectable_record_count"], 2)
        self.assertContains(response, 'x-data="{ selectedLeads: [], selectedOwners: [] }"')
        self.assertContains(response, 'name="owner_inquiry_ids"', count=2)
        self.assertContains(response, 'name="lead_ids"', count=2)
        self.assertContains(response, f"selectedOwners.includes('{inquiry.pk}')", count=2)
        self.assertContains(response, f"selectedLeads.includes('{lead.pk}')", count=2)
        self.assertContains(response, 'form="lead-bulk-form"', count=4)
        self.assertContains(response, 'selectedLeads.length + selectedOwners.length')
        self.assertContains(response, 'Выбрать все заявки на странице')

    def test_bulk_status_updates_both_request_types_with_matching_ids(self):
        inquiry = self.inquiry()
        lead = Lead.objects.create(
            owner=self.owner, name="Покупатель", phone="+79990000000",
            next_action="Позвонить", next_action_due_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(inquiry.pk, lead.pk)

        response = self.client.post(reverse("lead_bulk_status_update"), {
            "action": "status", "status": "read",
            "lead_ids": [lead.pk, lead.pk], "owner_inquiry_ids": [inquiry.pk, inquiry.pk],
        })

        self.assertRedirects(response, reverse("lead_list"))
        lead.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual(lead.status, "read")
        self.assertEqual(inquiry.status, "read")
        self.assertEqual(inquiry.result, "pending")
        self.assertIsNone(inquiry.first_contacted_at)
        filtered = self.client.get(reverse("lead_list"), {"status": "read"})
        self.assertEqual(len(filtered.context["leads"]), 2)
        self.assertEqual(next(tab["count"] for tab in filtered.context["status_tabs"] if tab["value"] == "read"), 2)

    def test_archived_owner_request_leaves_active_queues_and_can_be_restored(self):
        inquiry = self.inquiry()
        response = self.client.post(reverse("lead_bulk_status_update"), {
            "action": "delete", "owner_inquiry_ids": [inquiry.pk],
        })
        self.assertRedirects(response, reverse("lead_list"))
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, "archived")
        self.assertIsNotNone(inquiry.archived_at)
        self.assertEqual(self.client.get(reverse("lead_list"), {"queue": "needs_response"}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("lead_list"), {"status": "archived"}).context["page_obj"].paginator.count, 1)
        self.assertEqual(self.client.get(reverse("work_items"), {"kind": "owner"}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("owner_inquiry_list"), {"status": "active"}).context["page_obj"].paginator.count, 0)
        self.assertContains(self.client.get(reverse("owner_inquiry_detail", args=[inquiry.pk])), "Обращение в архиве")
        self.assertEqual(self.client.get(reverse("lead_list")).context["nav_new_leads_count"], 0)
        self.assertIsNone(self.client.get(reverse("client_detail", args=[inquiry.client_id])).context["priority_owner_inquiry"])

        self.client.post(reverse("lead_bulk_status_update"), {
            "action": "status", "status": "new", "owner_inquiry_ids": [inquiry.pk],
        })
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, "new")
        self.assertIsNone(inquiry.archived_at)
        self.assertEqual(self.client.get(reverse("lead_list"), {"queue": "needs_response"}).context["page_obj"].paginator.count, 1)

    def test_bulk_archive_rejects_team_agent_and_foreign_owner_request(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.agent, role="agent")
        inquiry = self.inquiry(agency=agency, assignee=self.agent)

        self.client.force_login(self.agent)
        self.assertNotContains(self.client.get(reverse("lead_list")), '<option value="archived">')
        response = self.client.post(reverse("lead_bulk_status_update"), {
            "action": "status", "status": "archived", "owner_inquiry_ids": [inquiry.pk],
        })
        self.assertEqual(response.status_code, 403)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, "new")
        self.client.post(reverse("lead_bulk_status_update"), {
            "action": "status", "status": "read", "owner_inquiry_ids": [inquiry.pk],
        })
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, "read")

        self.client.force_login(self.outsider)
        response = self.client.post(reverse("lead_bulk_status_update"), {
            "action": "status", "status": "new", "owner_inquiry_ids": [inquiry.pk],
        })
        self.assertEqual(response.status_code, 403)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, "read")

    def test_result_moves_request_between_work_groups(self):
        inquiry = self.inquiry()
        inquiry.first_contacted_at = timezone.now()
        inquiry.result = "follow_up"
        inquiry.next_action = "Обсудить договор"
        inquiry.save()
        self.assertEqual(self.client.get(reverse("lead_list"), {"queue": "in_progress"}).context["page_obj"].paginator.count, 1)
        inquiry.result = "declined"
        inquiry.next_action = ""
        inquiry.save()
        response = self.client.get(reverse("lead_list"), {"queue": "finished"})
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(response, "Отказ")
        self.assertEqual(self.client.get(reverse("lead_list"), {"queue": "in_progress"}).context["page_obj"].paginator.count, 0)

    def test_owner_and_regular_leads_share_pagination_without_duplicate_tasks(self):
        inquiry = self.inquiry()
        for index in range(10):
            Lead.objects.create(
                owner=self.owner, name=f"Покупатель {index}", phone="+79990000000",
                next_action="Позвонить", next_action_due_at=timezone.now() + timedelta(days=1),
            )
        first = self.client.get(reverse("lead_list"))
        second = self.client.get(reverse("lead_list"), {"page": 2})
        self.assertEqual(first.context["page_obj"].paginator.count, 11)
        self.assertEqual(len(first.context["leads"]), 10)
        self.assertEqual(len(second.context["leads"]), 1)
        self.assertEqual(sum(isinstance(item, OwnerInquiry) for item in first.context["leads"] + second.context["leads"]), 1)
        self.assertEqual(ClientReminder.objects.filter(lead__isnull=False).count(), 10)
        self.assertEqual(ClientReminder.objects.filter(client=inquiry.client).count(), 0)

    def test_team_visibility_and_mine_filter_use_owner_assignment(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.agent, role="agent")
        self.inquiry(agency=agency, assignee=self.agent)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("lead_list"), {"assigned": "mine"}).context["page_obj"].paginator.count, 1)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("lead_list")).context["page_obj"].paginator.count, 0)
