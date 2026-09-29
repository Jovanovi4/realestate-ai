from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Client, ClientInteraction, ClientReminder, Lead, Property


class LeadProcessingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("lead-handler", password="password")
        self.property = Property.objects.create(owner=self.owner, title="Квартира у парка")
        self.crm_client = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000111")
        self.lead = Lead.objects.create(
            property=self.property, client=self.crm_client, name="Анна", phone=self.crm_client.phone,
        )
        self.client.force_login(self.owner)

    def processing_data(self, **overrides):
        data = {
            "source": "landing",
            "qualification": "qualified",
            "next_action": "Позвонить после просмотра",
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "contact_method": "call",
            "contact_note": "Договорились о показе",
            "action": "contacted",
        }
        data.update(overrides)
        return data

    def completion_data(self, task, **overrides):
        data = {
            "task_id": task.pk,
            "outcome_note": "Клиент подтвердил интерес",
            "next_action": "Согласовать время показа",
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
        }
        data.update(overrides)
        return data

    def test_new_lead_has_responsible_person_and_30_minute_deadline(self):
        self.assertEqual(self.lead.assigned_to, self.owner)
        self.assertEqual(self.lead.source, "landing")
        self.assertEqual(self.lead.qualification, "pending")
        self.assertEqual(self.lead.next_action, "Связаться с клиентом")
        self.assertAlmostEqual(
            (self.lead.next_action_due_at - self.lead.created_at).total_seconds(), 1800, delta=2,
        )
        task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        self.assertEqual(task.owner, self.owner)
        self.assertEqual(task.client, self.crm_client)
        self.assertEqual(task.text, self.lead.next_action)
        self.assertEqual(task.due_at, self.lead.next_action_due_at)

    def test_editing_lead_updates_same_open_task(self):
        original_task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        response = self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data(action="save"))
        self.assertRedirects(response, reverse("lead_detail", args=[self.lead.pk]))
        original_task.refresh_from_db()
        self.assertEqual(original_task.text, "Позвонить после просмотра")
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead, is_done=False).count(), 1)

    def test_completing_action_keeps_outcome_and_creates_one_new_task(self):
        old_task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        url = reverse("lead_action_complete", args=[self.lead.pk])
        response = self.client.post(url, self.completion_data(old_task))
        self.assertRedirects(response, reverse("lead_detail", args=[self.lead.pk]))
        old_task.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertTrue(old_task.is_done)
        self.assertIsNotNone(old_task.completed_at)
        self.assertEqual(old_task.outcome_note, "Клиент подтвердил интерес")
        self.assertEqual(self.lead.next_action, "Согласовать время показа")
        new_task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        self.assertNotEqual(new_task.pk, old_task.pk)
        self.assertEqual(new_task.text, self.lead.next_action)
        self.assertTrue(ClientInteraction.objects.filter(lead=self.lead, text__contains="Клиент подтвердил интерес").exists())
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), "Клиент подтвердил интерес")

        response = self.client.post(url, self.completion_data(old_task))
        self.assertRedirects(response, reverse("lead_detail", args=[self.lead.pk]))
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead).count(), 2)
        self.assertEqual(ClientInteraction.objects.filter(lead=self.lead, text__contains="Клиент подтвердил интерес").count(), 1)

    def test_completion_requires_outcome_and_future_next_step(self):
        task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        url = reverse("lead_action_complete", args=[self.lead.pk])
        response = self.client.post(url, self.completion_data(task, outcome_note=""))
        self.assertContains(response, "Обязательное поле")
        response = self.client.post(url, self.completion_data(task, next_action_due_at="2020-01-01T10:00"))
        self.assertContains(response, "Укажите время в будущем")
        task.refresh_from_db()
        self.assertFalse(task.is_done)

    def test_archiving_cancels_task_and_restoring_creates_a_new_one(self):
        task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        self.lead.status = "archived"
        self.lead.save(update_fields=["status"])
        task.refresh_from_db()
        self.assertTrue(task.is_done)
        self.assertIsNotNone(task.cancelled_at)
        self.assertFalse(ClientReminder.objects.filter(lead=self.lead, is_done=False).exists())
        self.lead.status = "read"
        self.lead.save(update_fields=["status"])
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead, is_done=False).count(), 1)

    def test_linked_task_cannot_be_completed_without_outcome(self):
        task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        self.assertEqual(self.client.get(reverse("task_detail", args=[task.pk])).status_code, 404)
        self.assertRedirects(
            self.client.post(reverse("task_complete", args=[task.pk])),
            f"{reverse('lead_detail', args=[self.lead.pk])}?complete=1#lead-action",
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.post(reverse("client_reminder_complete", args=[self.crm_client.pk, task.pk])),
            f"{reverse('lead_detail', args=[self.lead.pk])}?complete=1#lead-action",
            fetch_redirect_response=False,
        )
        task.refresh_from_db()
        self.assertFalse(task.is_done)

    def test_workday_completion_link_opens_lead_form_without_duplicate_task(self):
        received_at = timezone.localtime(timezone.now()).replace(hour=12, minute=0, second=0, microsecond=0)
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=received_at, next_action_due_at=received_at + timedelta(minutes=30),
        )
        with patch("properties.workday_views.timezone.now", return_value=received_at + timedelta(minutes=5)):
            response = self.client.get(reverse("workday"))
        self.assertContains(response, f"{reverse('lead_detail', args=[self.lead.pk])}?complete=1#lead-action")
        self.assertNotContains(response, reverse("task_detail", args=[ClientReminder.objects.get(lead=self.lead).pk]))
        detail = self.client.get(reverse("lead_detail", args=[self.lead.pk]), {"complete": "1"})
        self.assertContains(detail, 'class="lead-action-complete" name="lead-action-editor" id="lead-action" open')

    def test_lead_card_shows_summary_and_keeps_editors_collapsed(self):
        response = self.client.get(reverse("lead_detail", args=[self.lead.pk]))
        self.assertContains(response, "Следующее действие")
        self.assertContains(response, "Ответить клиенту до")
        self.assertContains(response, 'class="lead-editor" name="lead-action-editor" id="lead-edit"')
        self.assertContains(response, 'class="lead-action-complete" name="lead-action-editor" id="lead-action"')
        self.assertContains(response, 'aria-label="Записать первый контакт" data-tooltip="Записать первый контакт"')
        self.assertContains(response, 'aria-label="Записать итог действия" data-tooltip="Записать итог действия"')
        self.assertContains(response, '<use href="#ui-icon-phone"></use>')
        self.assertContains(response, '<use href="#ui-icon-check"></use>')
        self.assertNotContains(response, 'id="lead-edit" open')
        self.assertNotContains(response, 'id="lead-action" open')
        self.assertContains(response, 'aria-label="Пояснение: Следующее действие"')
        self.assertContains(response, 'id="id_lead_next_action"')
        self.assertContains(response, 'id="id_complete_next_action"')

    def test_reply_queue_includes_read_lead_and_excludes_contacted_and_archived(self):
        self.lead.status = "read"
        self.lead.save(update_fields=["status"])
        response = self.client.get(reverse("lead_list"), {"queue": "reply"})
        self.assertEqual(list(response.context["leads"]), [self.lead])
        self.assertContains(response, "Требует ответа")
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        response = self.client.get(reverse("lead_list"), {"queue": "reply"})
        self.assertEqual(list(response.context["leads"]), [])
        self.lead.status = "archived"
        self.lead.save(update_fields=["status"])
        response = self.client.get(reverse("lead_list"), {"queue": "reply"})
        self.assertEqual(list(response.context["leads"]), [])

    def test_first_contact_is_recorded_once_and_next_action_is_required(self):
        url = reverse("lead_process", args=[self.lead.pk])
        response = self.client.post(url, self.processing_data())
        self.assertRedirects(response, reverse("lead_detail", args=[self.lead.pk]))
        self.lead.refresh_from_db()
        self.crm_client.refresh_from_db()
        self.assertIsNotNone(self.lead.first_contacted_at)
        self.assertEqual(self.lead.status, "read")
        self.assertEqual(self.crm_client.first_contacted_at, self.lead.first_contacted_at)
        self.assertEqual(self.lead.qualification, "qualified")
        self.assertEqual(self.lead.next_action, "Позвонить после просмотра")
        interaction = ClientInteraction.objects.get(lead=self.lead, interaction_type="call")
        self.assertEqual(interaction.text, "Договорились о показе")

        response = self.client.post(url, self.processing_data())
        self.assertRedirects(response, reverse("lead_detail", args=[self.lead.pk]))
        self.assertEqual(ClientInteraction.objects.filter(lead=self.lead, interaction_type="call").count(), 1)

        response = self.client.post(url, self.processing_data(next_action="", action="save"))
        self.assertContains(response, "Обязательное поле")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.next_action, "Позвонить после просмотра")

    def test_first_contact_requires_method_and_future_next_step(self):
        url = reverse("lead_process", args=[self.lead.pk])
        response = self.client.post(url, self.processing_data(contact_method=""))
        self.assertContains(response, "Укажите способ связи")
        response = self.client.post(url, self.processing_data(next_action_due_at="2020-01-01T10:00"))
        self.assertContains(response, "Укажите время в будущем")
        response = self.client.post(url, self.processing_data(qualification="pending"))
        self.assertContains(response, "Укажите результат квалификации")
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.first_contacted_at)

    def test_overdue_queue_and_today_show_assigned_next_step(self):
        Lead.objects.filter(pk=self.lead.pk).update(next_action_due_at=timezone.now() - timedelta(minutes=1))
        queue = self.client.get(reverse("lead_list"), {"queue": "overdue"})
        self.assertEqual(list(queue.context["leads"]), [self.lead])
        self.assertContains(queue, "Просрочено")
        today = self.client.get(reverse("workday"))
        self.assertContains(today, "Квартира у парка")
        self.assertEqual(today.context["nav_overdue_count"], 1)

    def test_rescheduling_next_action_does_not_hide_missed_first_response(self):
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=timezone.now() - timedelta(minutes=31),
            next_action_due_at=timezone.now() + timedelta(days=1),
        )
        queue = self.client.get(reverse("lead_list"), {"queue": "overdue"})
        self.assertEqual([lead.pk for lead in queue.context["leads"]], [self.lead.pk])
        detail = self.client.get(reverse("lead_detail", args=[self.lead.pk]))
        self.assertContains(detail, "Первый ответ просрочен")
        self.assertEqual(self.client.get(reverse("workday")).context["nav_overdue_count"], 1)

    def test_today_shows_first_response_even_if_next_action_is_tomorrow(self):
        received_at = timezone.localtime(timezone.now()).replace(hour=12, minute=0, second=0, microsecond=0)
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=received_at, next_action_due_at=received_at + timedelta(days=1),
        )
        with patch("properties.workday_views.timezone.now", return_value=received_at + timedelta(minutes=5)):
            response = self.client.get(reverse("workday"))
        self.assertContains(response, "Ответить клиенту")
        self.assertEqual([lead.pk for lead in response.context["today_leads"]], [self.lead.pk])

    def test_other_workspace_cannot_update_lead(self):
        other = User.objects.create_user("outsider", password="password")
        self.client.force_login(other)
        response = self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        self.assertEqual(response.status_code, 404)
