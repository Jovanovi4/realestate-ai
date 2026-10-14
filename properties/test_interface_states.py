from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Client, Deal, Lead, Property, Showing
from .services.crm_analytics_service import ANALYTICS_TZ


class InterfaceStateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("interface-owner", password="password")
        self.property = Property.objects.create(owner=self.user, title="Офис")
        self.client.force_login(self.user)

    def test_lead_work_groups_are_independent_of_read_and_archive(self):
        waiting_new = Lead.objects.create(property=self.property, name="Новая", phone="+70000000001")
        waiting_read = Lead.objects.create(property=self.property, name="Прочитана", phone="+70000000002", status="read")
        working = Lead.objects.create(property=self.property, name="В работе", phone="+70000000003", status="new")
        Lead.objects.filter(pk=working.pk).update(first_contacted_at=timezone.now())
        finished = Lead.objects.create(property=self.property, name="Завершена", phone="+70000000004", status="read")
        Lead.objects.filter(pk=finished.pk).update(outcome="declined")
        archived = Lead.objects.create(property=self.property, name="Архивная", phone="+70000000005", status="archived")

        default = self.client.get(reverse("lead_list"))
        self.assertEqual(
            {lead.pk for lead in default.context["leads"]},
            {waiting_new.pk, waiting_read.pk, working.pk, finished.pk, archived.pk},
        )
        self.assertEqual(
            {tab["value"]: tab["count"] for tab in default.context["queue_tabs"]},
            {"needs_response": 2, "in_progress": 1, "finished": 1},
        )
        self.assertEqual(
            {lead.pk for lead in self.client.get(reverse("lead_list"), {"queue": "needs_response"}).context["leads"]},
            {waiting_new.pk, waiting_read.pk},
        )
        self.assertEqual(
            [lead.pk for lead in self.client.get(reverse("lead_list"), {"queue": "in_progress"}).context["leads"]],
            [working.pk],
        )
        self.assertEqual(
            [lead.pk for lead in self.client.get(reverse("lead_list"), {"queue": "finished"}).context["leads"]],
            [finished.pk],
        )
        self.assertEqual(
            [lead.pk for lead in self.client.get(reverse("lead_list"), {"status": "archived"}).context["leads"]],
            [archived.pk],
        )

    def test_object_and_landing_states_do_not_change_card_completion(self):
        self.property.status = "published"
        self.property.save(update_fields=["status"])
        before = self.client.get(reverse("property_detail", args=[self.property.pk]))
        self.assertContains(before, "Объект: активен")
        self.assertContains(before, "Лендинг: не опубликован")
        self.assertContains(before, "Карточка:")
        self.assertIn("Укажите площадь объекта", [task["text"] for task in before.context["readiness_tasks"]])
        self.assertNotIn("Настройте и опубликуйте лендинг", [task["text"] for task in before.context["readiness_tasks"]])

        self.property.landing_published = True
        self.property.save(update_fields=["landing_published"])
        after = self.client.get(reverse("property_detail", args=[self.property.pk]))
        self.assertContains(after, "Лендинг: опубликован")
        self.assertEqual(before.context["readiness_completed"], after.context["readiness_completed"])

    def test_cancelled_showing_is_history_not_upcoming_or_overdue(self):
        client = Client.objects.create(owner=self.user, name="Анна", phone="+70000000006")
        deal = Deal.objects.create(owner=self.user, responsible=self.user, client=client, property=self.property)
        fixed_now = timezone.make_aware(datetime.combine(timezone.localdate(), time(12, 0)))
        upcoming = Showing.objects.create(owner=self.user, deal=deal, starts_at=fixed_now + timedelta(hours=1))
        cancelled = Showing.objects.create(owner=self.user, deal=deal, starts_at=fixed_now + timedelta(hours=2), status="cancelled")
        overdue = Showing.objects.create(owner=self.user, deal=deal, starts_at=fixed_now - timedelta(hours=1))

        with patch("properties.workday_views.timezone.now", return_value=fixed_now):
            response = self.client.get(reverse("workday"))
        self.assertEqual([item.pk for item in response.context["today_showings"]], [upcoming.pk])
        self.assertEqual([item.pk for item in response.context["today_other_showings"]], [cancelled.pk])
        self.assertIn(overdue.pk, [item.pk for item in response.context["overdue_showings"]])
        self.assertNotIn(cancelled.pk, [item.pk for item in response.context["overdue_showings"]])

    def test_empty_deal_board_has_one_compact_invitation(self):
        response = self.client.get(reverse("deal_board"))
        self.assertContains(response, "Сделок пока нет")
        self.assertContains(response, "Новая сделка")
        self.assertNotContains(response, 'data-deal-board data-mobile-board')

    def test_crm_and_analytics_use_the_same_timezone(self):
        self.assertEqual(settings.TIME_ZONE, ANALYTICS_TZ.key)
