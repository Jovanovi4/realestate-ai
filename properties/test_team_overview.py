from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientReminder, Lead, Property


class TeamWorkOverviewTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.owner = User.objects.create_user("overview-owner")
        self.manager = User.objects.create_user("overview-manager")
        self.agent = User.objects.create_user("overview-agent")
        self.outsider = User.objects.create_user("overview-outsider")
        self.agency = Agency.objects.create(owner=self.owner, name="Команда обзора")
        for user, role in ((self.owner, "owner"), (self.manager, "manager"), (self.agent, "agent")):
            AgencyMembership.objects.create(agency=self.agency, user=user, role=role)
        self.property = Property.objects.create(owner=self.owner, agency=self.agency, title="Общий объект")
        self.contact = Client.objects.create(owner=self.owner, agency=self.agency, name="Клиент", phone="+79990001111")

        self.unanswered = self.lead(self.owner)
        Lead.objects.filter(pk=self.unanswered.pk).update(
            created_at=self.now - timedelta(minutes=45), next_action_due_at=self.now + timedelta(hours=2),
        )
        self.overdue = self.lead(self.agent)
        Lead.objects.filter(pk=self.overdue.pk).update(
            first_contacted_at=self.now - timedelta(hours=1),
            next_action_due_at=self.now - timedelta(minutes=5),
        )
        self.unassigned = self.lead(self.owner)
        Lead.objects.filter(pk=self.unassigned.pk).update(
            assigned_to=None, created_at=self.now - timedelta(minutes=5),
            next_action_due_at=self.now + timedelta(hours=1),
        )
        self.manager_task = ClientReminder.objects.create(
            owner=self.manager, agency=self.agency, client=self.contact,
            text="Уточнить документы", due_at=self.now + timedelta(minutes=10),
        )

        demo = Property.objects.create(owner=self.owner, agency=self.agency, title="Демо", is_demo=True)
        Lead.objects.create(property=demo, client=None, name="Демо", phone="+79990002222")
        personal = Property.objects.create(owner=self.owner, title="Личный объект")
        Lead.objects.create(property=personal, client=None, name="Личный", phone="+79990004444")
        private = Property.objects.create(owner=self.outsider, title="Чужой объект")
        Lead.objects.create(property=private, client=None, name="Чужой", phone="+79990003333")
        archived = self.lead(self.owner)
        Lead.objects.filter(pk=archived.pk).update(assigned_to=None, status="archived")

    def lead(self, assignee):
        return Lead.objects.create(
            property=self.property, client=self.contact, assigned_to=assignee,
            name="Клиент", phone="+79990001111",
        )

    def test_cards_match_analytics_and_each_link_opens_the_matching_team_queue(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("agency_dashboard"))
        analytics = self.client.get(reverse("analytics"), {"scope": "team"})
        cards = {card["label"]: card for card in page.context["work_overview"]}

        self.assertEqual(cards["Без первого ответа"]["count"], analytics.context["report"]["now"]["unanswered"])
        self.assertEqual(cards["Просрочено"]["count"], analytics.context["report"]["now"]["overdue_leads"])
        self.assertEqual(cards["Без ответственного"]["count"], 1)
        expected = {
            "Без первого ответа": {self.unanswered.pk, self.unassigned.pk},
            "Просрочено": {self.unanswered.pk, self.overdue.pk},
            "Без ответственного": {self.unassigned.pk},
        }
        for label, ids in expected.items():
            with self.subTest(label=label):
                response = self.client.get(cards[label]["url"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual({lead.pk for lead in response.context["lead_page"]}, ids)
        self.assertContains(page, 'class="agency-work-overview__grid"')
        self.assertContains(page, '<details class="agency-work-overview__mobile">')
        self.assertContains(page, f'aria-label="Просрочено: {cards["Просрочено"]["count"]}"')

    def test_member_rows_use_analytics_overdue_count_and_nearest_action(self):
        self.client.force_login(self.manager)
        page = self.client.get(reverse("agency_dashboard"))
        analytics = self.client.get(reverse("analytics"), {"scope": "team"})
        rows = {row["user_id"]: row for row in page.context["work_members"]}
        analytics_rows = {row["user_id"]: row for row in analytics.context["member_rows"] if row["user_id"]}

        self.assertEqual(rows[self.agent.pk]["overdue"], analytics_rows[self.agent.pk]["overdue"])
        self.assertEqual(rows[self.agent.pk]["urgent_work"], analytics_rows[self.agent.pk]["urgent_work"])
        self.assertEqual(rows[self.agent.pk]["next_due_at"], self.now - timedelta(minutes=5))
        self.assertEqual(rows[self.owner.pk]["next_due_at"], self.now - timedelta(minutes=15))
        self.assertEqual(rows[self.manager.pk]["next_due_at"], self.manager_task.due_at)
        self.assertEqual(rows[self.manager.pk]["membership"].role, "manager")
        workload = self.client.get(rows[self.agent.pk]["url"])
        self.assertEqual({lead.pk for lead in workload.context["lead_page"]}, {self.overdue.pk})

    def test_agent_and_outsider_do_not_receive_team_aggregates(self):
        self.client.force_login(self.agent)
        agent_page = self.client.get(reverse("agency_dashboard"))
        self.assertIsNone(agent_page.context["work_overview"])
        self.assertFalse(agent_page.context["work_members"])
        self.assertNotContains(agent_page, "Требует внимания")

        self.client.force_login(self.outsider)
        outsider_page = self.client.get(reverse("agency_dashboard"))
        self.assertEqual(outsider_page.status_code, 200)
        self.assertNotContains(outsider_page, "Требует внимания")
