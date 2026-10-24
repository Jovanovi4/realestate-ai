from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from billing.enforcement import TariffDenied

from .models import Agency, AgencyMembership, Client, ClientReminder, Lead, Property


class ClientPanelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("client-panel-user", password="password")
        self.contact = Client.objects.create(
            owner=self.user, name="Анна", phone="+79990000111", source="recommendation",
            preferred_area="Центр", preferred_contact_time="После 18:00",
        )
        self.client.force_login(self.user)

    def panel_post(self, url, data):
        return self.client.post(url, {
            "panel_context": "client_list",
            "panel_list_query": "status=new&q=Анна&page=1",
            **data,
        }, HTTP_HX_REQUEST="true")

    def test_list_opens_client_panel_and_preserves_filter_link(self):
        response = self.client.get(reverse("client_list"), {
            "status": "new", "q": "Анна", "client": self.contact.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.count(b'data-client-open-row'), 2)
        self.assertRegex(response.content.decode(), r'<tr class="lead-table__row is-open[^\"]*"[^>]*data-client-open-row>')
        self.assertContains(response, 'id="client-panel"')
        self.assertContains(response, 'class="lead-workspace client-workspace client-workspace--open"')
        self.assertContains(response, "Отношения")
        self.assertContains(response, 'class="detail-status-control__editor"')
        self.assertContains(response, 'class="detail-status-control__current"')
        self.assertContains(response, 'data-client-panel-status-form data-initial-status="new"')
        self.assertContains(response, 'data-panel-status-save')
        self.assertContains(response, "Запланируйте контакт")
        self.assertContains(response, "После 18:00")
        self.assertContains(response, "О клиенте и история")
        self.assertContains(response, reverse("client_detail", args=[self.contact.pk]))
        self.assertContains(response, "q=%D0%90%D0%BD%D0%BD%D0%B0&amp;status=new&amp;page=1")
        self.assertContains(response, 'id="client-panel-discard"')

        fragment = self.client.get(reverse("client_list"), {"client": self.contact.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(fragment, 'id="client-workspace"')
        self.assertNotContains(fragment, "<h1")
        restored = self.client.get(reverse("client_list"), {"client": self.contact.pk},
                                   HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true")
        self.assertContains(restored, "<h1")

    def test_next_client_fragment_keeps_open_workspace_and_selected_row(self):
        next_client = Client.objects.create(
            owner=self.user, name="Борис", phone="+79990000222", source="recommendation",
        )
        first = self.client.get(reverse("client_list"), {"client": self.contact.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(first, f'data-next-client-id="{next_client.pk}"')
        self.assertContains(first, f'data-client-panel-open="{next_client.pk}"')

        response = self.client.get(reverse("client_list"), {"client": next_client.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(response, 'class="lead-workspace client-workspace client-workspace--open"')
        self.assertContains(response, '<h2 id="client-panel-title">Борис</h2>')
        self.assertContains(response, f'data-client-panel-open="{next_client.pk}" aria-current="true"')

    def test_panel_status_uses_existing_relationship_rules_and_refreshes_list(self):
        url = reverse("client_status_update", args=[self.contact.pk])
        invalid = self.panel_post(url, {"status": "unknown"})
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Выберите состояние отношений")
        self.assertContains(invalid, 'id="client-panel"')
        self.assertContains(invalid, 'data-unsaved="true"')

        changed = self.panel_post(url, {"status": "in_progress"})
        self.assertEqual(changed.status_code, 204)
        self.assertIn("clientPanelSaved", changed.headers["HX-Trigger"])
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, "in_progress")
        refreshed = self.client.get(reverse("client_list"), {"client": self.contact.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(refreshed, "В работе")

        property = Property.objects.create(owner=self.user, title="Квартира")
        Lead.objects.create(property=property, client=self.contact, name=self.contact.name, phone=self.contact.phone)
        blocked = self.panel_post(url, {"status": "inactive"})
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "открытая заявка")
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, "in_progress")

    def test_task_can_be_created_and_edited_inline_without_changing_its_links(self):
        created = self.panel_post(reverse("client_reminder_create", args=[self.contact.pk]), {
            "text": "Позвонить Анне", "due_at": "2026-10-12T12:00",
        })
        self.assertEqual(created.status_code, 204)
        reminder = ClientReminder.objects.get(client=self.contact)
        self.assertEqual(reminder.owner, self.user)

        response = self.client.get(reverse("client_list"), {"client": self.contact.pk})
        self.assertContains(response, "Позвонить Анне")
        self.assertContains(response, "Изменить задачу")
        url = reverse("client_panel_task_update", args=[self.contact.pk, reminder.pk])
        invalid = self.panel_post(url, {"text": "", "due_at": ""})
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Обязательное поле")
        reminder.refresh_from_db()
        self.assertEqual(reminder.text, "Позвонить Анне")

        changed = self.panel_post(url, {"text": "Написать Анне", "due_at": "2026-10-13T13:00"})
        self.assertEqual(changed.status_code, 204)
        reminder.refresh_from_db()
        self.assertEqual(reminder.text, "Написать Анне")
        self.assertEqual(reminder.client, self.contact)
        self.assertEqual(reminder.owner, self.user)
        self.assertIsNone(reminder.lead_id)

        completed = self.panel_post(reverse("client_reminder_complete", args=[self.contact.pk, reminder.pk]), {})
        self.assertEqual(completed.status_code, 204)
        reminder.refresh_from_db()
        self.assertTrue(reminder.is_done)
        self.assertIsNotNone(reminder.completed_at)

    def test_lead_first_contact_stays_in_lead_workflow(self):
        property = Property.objects.create(owner=self.user, title="Квартира")
        lead = Lead.objects.create(property=property, client=self.contact, name=self.contact.name, phone=self.contact.phone)
        reminder = ClientReminder.objects.get(lead=lead)
        response = self.client.get(reverse("client_list"), {"client": self.contact.pk})
        self.assertContains(response, "Записать первый контакт")
        self.assertContains(response, reverse("lead_detail", args=[lead.pk]))
        self.assertContains(response, 'class="lead-panel__focus client-panel__focus-card"')
        self.assertContains(response, f'href="{reverse("lead_detail", args=[lead.pk])}?contact=1#lead-edit">Записать первый контакт</a>')
        self.assertNotContains(response, ">К заявке</a>")
        self.assertNotContains(response, reverse("client_panel_task_update", args=[self.contact.pk, reminder.pk]))
        self.assertEqual(self.panel_post(
            reverse("client_panel_task_update", args=[self.contact.pk, reminder.pk]),
            {"text": "Обойти заявку", "due_at": "2026-10-13T13:00"},
        ).status_code, 404)

    def test_lead_reminder_opens_from_whole_next_action_block(self):
        property = Property.objects.create(owner=self.user, title="Квартира")
        lead = Lead.objects.create(property=property, client=self.contact, name=self.contact.name, phone=self.contact.phone)
        lead.first_contacted_at = timezone.now()
        lead.save(update_fields=["first_contacted_at"])
        reminder = ClientReminder.objects.get(lead=lead)
        response = self.client.get(reverse("client_list"), {"client": self.contact.pk})
        self.assertContains(response, 'class="lead-panel__focus client-panel__focus-card"')
        self.assertContains(response, f'href="{reverse("lead_detail", args=[lead.pk])}?complete=1#lead-action">{reminder.text}</a>')
        self.assertNotContains(response, ">К заявке</a>")

    def test_task_completion_keeps_client_list_without_task_summary(self):
        reminder = ClientReminder.objects.create(
            owner=self.user, client=self.contact, text="Срочно позвонить",
            due_at=timezone.now() - timedelta(hours=1),
        )
        before = self.client.get(reverse("client_list"), {"client": self.contact.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(before, "Срочно позвонить")
        self.assertNotContains(before, "Задачи по клиентам")
        completed = self.panel_post(reverse("client_reminder_complete", args=[self.contact.pk, reminder.pk]), {})
        self.assertEqual(completed.status_code, 204)
        after = self.client.get(reverse("client_list"), {"client": self.contact.pk}, HTTP_HX_REQUEST="true")
        self.assertNotContains(after, "Срочно позвонить")
        self.assertNotContains(after, "Задачи по клиентам")

    def test_other_workspace_and_owner_tab_boundaries(self):
        outsider = User.objects.create_user("client-panel-outsider", password="password")
        foreign = Client.objects.create(owner=outsider, name="Чужой", phone="+79990000222")
        self.assertEqual(self.client.get(reverse("client_list"), {"client": foreign.pk}).status_code, 404)
        self.assertEqual(self.panel_post(reverse("client_status_update", args=[foreign.pk]),
                                         {"status": "in_progress"}).status_code, 404)
        owner_only = Client.objects.create(owner=self.user, name="Собственник", phone="+79990000333", contact_role="owner")
        self.assertEqual(self.client.get(reverse("client_list"), {"client": owner_only.pk}).status_code, 404)
        owner_response = self.client.get(reverse("client_list"), {"role": "owner", "client": owner_only.pk})
        self.assertContains(owner_response, 'id="client-panel"')

    def test_tariff_denial_is_shown_in_panel_without_mutation(self):
        denied = TariffDenied(SimpleNamespace(code="feature_not_available", message="Действие недоступно по тарифу."))
        with patch("properties.views.require_resource_action", side_effect=denied):
            response = self.panel_post(reverse("client_status_update", args=[self.contact.pk]),
                                       {"status": "in_progress"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Действие недоступно по тарифу")
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, "new")

    def test_task_edit_requires_its_assigned_user(self):
        other = User.objects.create_user("another-task-owner", password="password")
        agency = Agency.objects.create(owner=self.user, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        AgencyMembership.objects.create(agency=agency, user=other, role="agent")
        team_contact = Client.objects.create(
            owner=self.user, agency=agency, name="Командный клиент", phone="+79990000444",
        )
        reminder = ClientReminder.objects.create(
            owner=other, agency=agency, client=team_contact, text="Позвонить", due_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.get(reverse("client_list"), {"client": team_contact.pk})
        self.assertContains(response, "Позвонить")
        self.assertNotContains(response, reverse("client_panel_task_update", args=[team_contact.pk, reminder.pk]))
        self.assertEqual(self.panel_post(
            reverse("client_panel_task_update", args=[team_contact.pk, reminder.pk]),
            {"text": "Изменить чужую задачу", "due_at": "2026-10-13T13:00"},
        ).status_code, 404)
