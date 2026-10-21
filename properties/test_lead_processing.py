from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Client, ClientInteraction, ClientReminder, Deal, Lead, Property


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

    def test_lead_list_opens_compact_panel_with_first_contact_as_main_action(self):
        response = self.client.get(reverse("lead_list"), {"lead": self.lead.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.count(b'data-lead-open-row'), 2)
        self.assertRegex(response.content.decode(), r'<tr class="lead-table__row is-open[^\"]*"[^>]*data-lead-open-row>')
        self.assertContains(response, 'id="lead-panel"')
        self.assertContains(response, 'class="lead-workspace lead-workspace--open"')
        self.assertContains(response, "Записать первый контакт")
        self.assertNotContains(response, "Записать итог</span>")
        self.assertContains(response, self.lead.phone)
        self.assertContains(response, self.property.title)
        self.assertContains(response, "Ответственный:")
        self.assertContains(response, "Первый ответ до")
        self.assertContains(response, "Изменить план")
        self.assertContains(response, "Перевести в сделку")
        self.assertContains(response, "Закрыть без сделки")
        self.assertNotContains(response, "Об обращении")
        self.assertNotContains(response, "История действий")
        self.assertContains(response, 'id="lead-panel-discard"')
        self.assertContains(response, "Есть несохранённые изменения")

    def test_lead_search_precedes_filter_toggle_and_queues_stay_collapsed(self):
        response = self.client.get(reverse("lead_list"), {"q": "Анна"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["lead_filter_count"], 1)
        html = response.content.decode()
        self.assertIn('data-bs-target="#lead-filter-panel"', html)
        self.assertIn('aria-expanded="false" aria-controls="lead-filter-panel"', html)
        self.assertLess(html.index('id="lead-search-query"'), html.index('data-lead-filter-toggle'))
        self.assertLess(html.index('data-lead-filter-toggle'), html.index('id="lead-filter-panel"'))
        self.assertLess(html.index('id="lead-search-query"'), html.index('aria-label="Рабочие группы заявок"'))
        self.assertLess(html.index('aria-label="Рабочие группы заявок"'), html.index('id="leads-results"'))
        self.assertContains(response, self.lead.name)

    def test_lead_list_without_filters_shows_all_work_groups(self):
        contacted = Lead.objects.create(
            property=self.property, name="Борис", phone="+79990000444",
        )
        Lead.objects.filter(pk=contacted.pk).update(first_contacted_at=timezone.now())

        response = self.client.get(reverse("lead_list"))

        self.assertEqual(response.context["selected_queue"], "")
        self.assertEqual(response.context["lead_filter_count"], 0)
        self.assertEqual({lead.pk for lead in response.context["leads"]}, {self.lead.pk, contacted.pk})
        self.assertIn("queue=all", response.context["lead_panel_query"])

        awaiting = self.client.get(reverse("lead_list"), {"queue": "needs_response"})
        self.assertEqual(awaiting.context["selected_queue"], "needs_response")
        self.assertEqual([lead.pk for lead in awaiting.context["leads"]], [self.lead.pk])

    def test_opening_lead_keeps_explicit_all_queue_after_filters_are_cleared(self):
        answered = Lead.objects.create(
            property=self.property, name="Ответ получен", phone="+79990000444",
        )
        Lead.objects.filter(pk=answered.pk).update(first_contacted_at=timezone.now())
        url = reverse("lead_list")

        listing = self.client.get(url, {"queue": "all"})
        self.assertEqual(listing.context["selected_queue"], "")
        self.assertEqual({lead.pk for lead in listing.context["leads"]}, {self.lead.pk, answered.pk})
        self.assertIn("queue=all", listing.context["lead_panel_query"])
        self.assertIn("queue=all", listing.context["pagination_query"])
        self.assertContains(listing, f"&amp;lead={answered.pk}")

        opened = self.client.get(url, {"queue": "all", "page": 1, "lead": answered.pk})
        self.assertEqual(opened.context["selected_queue"], "")
        self.assertEqual({lead.pk for lead in opened.context["leads"]}, {self.lead.pk, answered.pk})
        self.assertEqual(opened.context["selected_lead"].pk, answered.pk)
        self.assertContains(opened, 'aria-label="Закрыть заявку"')
        self.assertIn("queue=all", opened.context["lead_panel_query"])

        searched = self.client.get(url, {"queue": "all", "q": "Ответ"})
        self.assertIn("queue=all", searched.context["clear_search_url"])

    def test_panel_offers_next_visible_lead_and_refreshes_queue_counts(self):
        other = Lead.objects.create(property=self.property, name="Борис", phone="+79990000222")
        url = reverse("lead_list")
        response = self.client.get(url, {"status": "new", "lead": self.lead.pk})
        self.assertContains(response, 'data-new-leads-count="2"')
        self.assertContains(response, f'data-lead-panel-open="{other.pk}"')
        self.assertContains(response, 'aria-label="Открыть следующую заявку"')
        self.assertEqual(next(tab["count"] for tab in response.context["status_tabs"] if tab["value"] == "new"), 2)

        self.client.post(reverse("lead_process", args=[self.lead.pk]), {
            **self.processing_data(), "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        refreshed = self.client.get(url, {"status": "new", "lead": self.lead.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(refreshed, 'data-new-leads-count="1"')
        self.assertContains(refreshed, f'data-lead-panel-open="{other.pk}"')
        self.assertContains(refreshed, f'data-next-lead-id="{other.pk}"')
        self.assertEqual(next(tab["count"] for tab in refreshed.context["status_tabs"] if tab["value"] == "new"), 1)
        self.assertContains(refreshed, 'id="lead-panel"')

    def test_overdue_lead_stays_visibly_urgent_in_list_and_panel(self):
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=now - timedelta(minutes=50),
            next_action_due_at=now - timedelta(minutes=20),
        )

        response = self.client.get(reverse("lead_list"), {"lead": self.lead.pk})
        self.assertContains(response, "is-urgent")
        self.assertContains(response, "Первый ответ просрочен")
        self.assertContains(response, 'data-overdue-count="1"')

    def test_lead_panel_switches_main_action_after_first_contact(self):
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())

        response = self.client.get(reverse("lead_list"), {"lead": self.lead.pk}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="lead-panel"')
        self.assertContains(response, "Записать итог</span>")
        self.assertNotContains(response, "Записать первый контакт")
        self.assertContains(response, 'name="task_id"')

    def test_lead_panel_does_not_open_another_workspace_lead(self):
        another_owner = User.objects.create_user("other-lead-owner", password="password")
        another_property = Property.objects.create(owner=another_owner, title="Чужой объект")
        another_lead = Lead.objects.create(property=another_property, name="Чужой клиент", phone="+79990000222")

        response = self.client.get(reverse("lead_list"), {"lead": another_lead.pk})

        self.assertEqual(response.status_code, 404)
        post = self.client.post(reverse("lead_process", args=[another_lead.pk]), {
            **self.processing_data(), "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(post.status_code, 404)
        another_lead.refresh_from_db()
        self.assertIsNone(another_lead.first_contacted_at)

    def test_lead_panel_keeps_both_overdue_deadlines_distinct(self):
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=now - timedelta(minutes=90),
            next_action_due_at=now - timedelta(minutes=70),
        )

        response = self.client.get(reverse("lead_list"), {"lead": self.lead.pk})

        self.assertContains(response, "Задача просрочена")
        self.assertContains(response, "Первый ответ просрочен")

    def test_lead_panel_shows_one_warning_for_deadlines_in_same_minute(self):
        response_due = timezone.now().replace(second=30, microsecond=0) - timedelta(hours=1)
        for offset_seconds, primary_warning, duplicate_warning in (
            (20, "Первый ответ просрочен", "Задача просрочена"),
            (-20, "Задача просрочена", "Первый ответ просрочен"),
        ):
            with self.subTest(offset_seconds=offset_seconds):
                Lead.objects.filter(pk=self.lead.pk).update(
                    created_at=response_due - timedelta(minutes=30),
                    next_action_due_at=response_due + timedelta(seconds=offset_seconds),
                )
                response = self.client.get(reverse("lead_list"), {"lead": self.lead.pk})
                panel = response.content.decode().split('<aside class="lead-panel" id="lead-panel"', 1)[1].split("</aside>", 1)[0]
                self.assertEqual(panel.count(primary_warning), 1)
                self.assertNotIn(duplicate_warning, panel)

    def test_panel_deep_link_preserves_filters_and_full_card_link(self):
        url = reverse("lead_list")
        response = self.client.get(url, {"q": "Анна", "status": "new", "page": "1", "lead": self.lead.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'aria-current="true">{self.lead.name}</a>')
        self.assertContains(response, f'href="?status=new&amp;q=%D0%90%D0%BD%D0%BD%D0%B0&amp;page=1"')
        self.assertContains(response, reverse("lead_detail", args=[self.lead.pk]))

    def test_history_restore_request_returns_full_page_for_back_forward(self):
        response = self.client.get(
            reverse("lead_list"), {"lead": self.lead.pk},
            HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<html')
        self.assertContains(response, 'id="lead-panel"')

    def test_panel_contact_post_updates_same_lead_without_redirect(self):
        url = reverse("lead_process", args=[self.lead.pk])
        data = {**self.processing_data(), "panel_context": "lead_list", "panel_list_query": "status=new&page=1"}
        response = self.client.post(url, data, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 204)
        self.assertIn("leadPanelSaved", response.headers["HX-Trigger"])
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.first_contacted_at)
        self.assertEqual(self.lead.next_action, data["next_action"])
        refreshed = self.client.get(reverse("lead_list"), {"lead": self.lead.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(refreshed, "Записать итог</span>")
        self.assertContains(refreshed, data["next_action"])

    def test_panel_invalid_contact_shows_field_error_and_keeps_list_query(self):
        response = self.client.post(reverse("lead_process", args=[self.lead.pk]), {
            **self.processing_data(next_action_due_at="2020-01-01T10:00"),
            "panel_context": "lead_list", "panel_list_query": "queue=all&q=%D0%90%D0%BD%D0%BD%D0%B0&page=2",
        }, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="lead-panel"')
        self.assertContains(response, "Укажите время в будущем")
        self.assertContains(response, 'queue=all&amp;q=%D0%90%D0%BD%D0%BD%D0%B0&amp;page=2')
        self.assertContains(response, 'data-unsaved="true"')
        self.assertNotContains(response, 'id="lead-workspace"')
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.first_contacted_at)

    def test_panel_action_result_and_decline_use_existing_workflow(self):
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        response = self.client.post(reverse("lead_action_complete", args=[self.lead.pk]), {
            **self.completion_data(task), "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        task.refresh_from_db()
        self.assertTrue(task.is_done)
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead, is_done=False).count(), 1)

        response = self.client.post(reverse("lead_outcome", args=[self.lead.pk]), {
            "action": "decline", "reason": "", "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Обязательное поле")
        self.assertContains(response, 'class="lead-panel__action lead-panel__decline" open')

        response = self.client.post(reverse("lead_outcome", args=[self.lead.pk]), {
            "action": "decline", "reason": "Клиент отказался", "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.outcome, "declined")
        declined_list = self.client.get(reverse("lead_list"), {"lead": self.lead.pk})
        self.assertContains(declined_list, 'lead-outcome-badge--declined')
        self.assertContains(declined_list, "Клиент отказался")
        self.assertNotContains(declined_list, 'lead-outcome-text--declined')

    def test_panel_keeps_plan_editor_after_first_contact(self):
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        panel = self.client.get(reverse("lead_list"), {"lead": self.lead.pk})
        self.assertContains(panel, "Изменить план")
        self.assertContains(panel, 'name="action" value="save"')

        response = self.client.post(reverse("lead_process", args=[self.lead.pk]), {
            **self.processing_data(action="save", next_action="Отправить документы"),
            "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.next_action, "Отправить документы")
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).text, "Отправить документы")

    def test_panel_can_attach_qualified_lead_to_existing_deal(self):
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        deal = Deal.objects.create(
            owner=self.owner, responsible=self.owner, client=self.crm_client, property=self.property,
        )
        response = self.client.post(reverse("lead_outcome", args=[self.lead.pk]), {
            "action": "convert_existing", "deal_id": deal.pk, "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 204)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.outcome, "converted")
        self.assertEqual(self.lead.outcome_deal, deal)
        self.assertFalse(ClientReminder.objects.filter(lead=self.lead, is_done=False).exists())
        panel = self.client.get(reverse("lead_list"), {"lead": self.lead.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(panel, 'lead-outcome-badge--converted')
        self.assertNotContains(panel, 'lead-outcome-text--converted')
        self.assertNotContains(panel, "Записать итог</span>")

    def test_panel_archive_updates_lead_and_normal_post_still_redirects(self):
        response = self.client.post(reverse("lead_delete", args=[self.lead.pk]), {
            "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "archived")
        self.assertRedirects(
            self.client.post(reverse("lead_delete", args=[self.lead.pk])),
            f"{reverse('lead_list')}?status=archived",
        )

    def test_editing_lead_updates_same_open_task(self):
        original_task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        response = self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data(action="save"))
        self.assertRedirects(response, reverse("lead_detail", args=[self.lead.pk]))
        original_task.refresh_from_db()
        self.assertEqual(original_task.text, "Позвонить после просмотра")
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead, is_done=False).count(), 1)

    def test_first_contact_from_client_card_updates_lead_client_and_today(self):
        another_property = Property.objects.create(owner=self.owner, title="Вторая квартира")
        another_lead = Lead.objects.create(
            property=another_property, client=self.crm_client, name="Анна", phone=self.crm_client.phone,
        )
        # Keep this lead due in the current workday even when the test runs near midnight.
        Lead.objects.filter(pk=another_lead.pk).update(next_action_due_at=timezone.now() - timedelta(minutes=1))
        client_url = reverse("client_detail", args=[self.crm_client.pk])
        page = self.client.get(client_url)
        self.assertContains(page, reverse("lead_process", args=[self.lead.pk]))
        self.assertContains(page, "Первый контакт · Квартира у парка")

        response = self.client.post(reverse("lead_process", args=[self.lead.pk]), {
            **self.processing_data(), "return_to_client": "1",
        })
        self.assertRedirects(response, f"{client_url}#client-leads", fetch_redirect_response=False)
        self.lead.refresh_from_db()
        self.crm_client.refresh_from_db()
        another_lead.refresh_from_db()
        self.assertIsNotNone(self.lead.first_contacted_at)
        self.assertEqual(self.crm_client.first_contacted_at, self.lead.first_contacted_at)
        self.assertEqual(self.crm_client.status, "in_progress")
        self.assertIsNone(another_lead.first_contacted_at)
        self.assertEqual(ClientInteraction.objects.filter(lead=self.lead, interaction_type="call").count(), 1)
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).text, "Позвонить после просмотра")
        self.assertContains(self.client.get(reverse("work_items")), "Позвонить после просмотра")
        today = self.client.get(reverse("workday"))
        self.assertIn(another_lead, list(today.context["today_leads"]) + list(today.context["overdue_leads"]))

        self.client.post(reverse("lead_process", args=[self.lead.pk]), {
            **self.processing_data(), "return_to_client": "1",
        })
        self.assertEqual(ClientInteraction.objects.filter(lead=self.lead, interaction_type="call").count(), 1)

    def test_client_card_shows_first_contact_as_main_step_and_other_tasks_once(self):
        other_task = ClientReminder.objects.create(
            owner=self.owner, client=self.crm_client, text="Отправить подборку",
            due_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.get(reverse("client_detail", args=[self.crm_client.pk]))

        self.assertContains(response, 'class="btn btn-neutral">Новая сделка</a>')
        self.assertContains(response, f"{reverse('lead_detail', args=[self.lead.pk])}?contact=1#lead-edit")
        self.assertContains(response, "Остальные задачи")
        self.assertNotContains(response, 'class="client-priority-lead')
        self.assertEqual([task.pk for task in response.context["reminders"]], [other_task.pk])
        self.assertEqual(response.context["pending_reminders_count"], 1)

        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        response = self.client.get(reverse("client_detail", args=[self.crm_client.pk]))
        self.assertIsNone(response.context["priority_lead"])
        self.assertContains(response, 'class="btn btn-primary">Новая сделка</a>')
        self.assertEqual([task.pk for task in response.context["reminders"]], [other_task.pk])

    def test_invalid_client_card_contact_keeps_form_and_does_not_mark_lead(self):
        response = self.client.post(reverse("lead_process", args=[self.lead.pk]), {
            **self.processing_data(next_action_due_at="2020-01-01T10:00"), "return_to_client": "1",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Укажите время в будущем")
        self.assertContains(response, 'id="client-leads"')
        self.assertTrue(response.context["open_client_leads_tab"])
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.first_contacted_at)
        self.assertEqual(ClientInteraction.objects.filter(lead=self.lead).count(), 0)

    def test_client_history_cannot_bypass_first_contact_on_open_lead(self):
        client_url = reverse("client_detail", args=[self.crm_client.pk])
        page = self.client.get(client_url)
        choices = [value for value, _ in page.context["interaction_form"].fields["interaction_type"].choices]
        self.assertNotIn("call", choices)
        self.assertNotIn("message", choices)
        self.assertContains(page, "Звонок или сообщение по новой заявке?")
        self.assertContains(page, 'form="client-history-form" name="intent" value="first_contact"')

        via_guide = self.client.post(reverse("client_interaction_create", args=[self.crm_client.pk]), {
            "interaction_type": "note", "text": "Предварительно обсудили условия", "intent": "first_contact",
        })
        self.assertEqual(via_guide.status_code, 200)
        self.assertEqual(via_guide.context["contact_forms"][0]["form"].initial["contact_method"], "call")
        self.assertEqual(via_guide.context["contact_forms"][0]["form"].initial["contact_note"], "Предварительно обсудили условия")

        response = self.client.post(reverse("client_interaction_create", args=[self.crm_client.pk]), {
            "interaction_type": "call", "text": "Поговорили",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["open_client_leads_tab"])
        self.assertContains(response, "Текст и способ связи перенесены")
        draft = response.context["contact_forms"][0]
        self.assertEqual(draft["lead"].pk, self.lead.pk)
        self.assertEqual(draft["form"].initial["contact_method"], "call")
        self.assertEqual(draft["form"].initial["contact_note"], "Поговорили")
        self.lead.refresh_from_db()
        self.crm_client.refresh_from_db()
        self.assertIsNone(self.lead.first_contacted_at)
        self.assertIsNone(self.crm_client.first_contacted_at)
        self.assertEqual(ClientInteraction.objects.filter(client=self.crm_client).count(), 0)

    def test_history_draft_keeps_note_until_user_chooses_one_of_two_leads(self):
        another_property = Property.objects.create(owner=self.owner, title="Вторая квартира")
        another_lead = Lead.objects.create(
            property=another_property, client=self.crm_client, name="Анна", phone=self.crm_client.phone,
        )
        response = self.client.post(reverse("client_interaction_create", args=[self.crm_client.pk]), {
            "interaction_type": "message", "text": "Обсудили оба объекта, ждём решение",
        })
        self.assertEqual(response.status_code, 200)
        drafts = {item["lead"].pk: item for item in response.context["contact_forms"]}
        self.assertEqual(set(drafts), {self.lead.pk, another_lead.pk})
        for item in drafts.values():
            self.assertEqual(item["form"].initial["contact_method"], "message")
            self.assertEqual(item["form"].initial["contact_note"], "Обсудили оба объекта, ждём решение")
        self.assertFalse(Lead.objects.filter(pk__in=drafts, first_contacted_at__isnull=False).exists())
        self.assertFalse(ClientInteraction.objects.filter(client=self.crm_client).exists())

        self.client.post(reverse("lead_process", args=[another_lead.pk]), {
            **self.processing_data(
                contact_method=drafts[another_lead.pk]["form"].initial["contact_method"],
                contact_note=drafts[another_lead.pk]["form"].initial["contact_note"],
            ),
            "return_to_client": "1",
            "leads_page": drafts[another_lead.pk]["page_number"],
        })
        self.lead.refresh_from_db()
        another_lead.refresh_from_db()
        self.assertIsNone(self.lead.first_contacted_at)
        self.assertIsNotNone(another_lead.first_contacted_at)
        self.assertTrue(ClientInteraction.objects.filter(
            lead=another_lead, interaction_type="message", text="Обсудили оба объекта, ждём решение",
        ).exists())

    def test_lead_list_prioritizes_missed_first_response_then_overdue_work(self):
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=now - timedelta(hours=2), next_action_due_at=now + timedelta(days=1),
        )
        overdue_action = Lead.objects.create(
            property=self.property, client=self.crm_client, name="Просрочен шаг", phone=self.crm_client.phone,
        )
        Lead.objects.filter(pk=overdue_action.pk).update(
            first_contacted_at=now - timedelta(minutes=10), next_action_due_at=now - timedelta(minutes=1),
        )
        awaiting = Lead.objects.create(
            property=self.property, client=self.crm_client, name="Ждёт ответа", phone=self.crm_client.phone,
        )
        Lead.objects.filter(pk=awaiting.pk).update(next_action_due_at=now + timedelta(days=1))
        planned = Lead.objects.create(
            property=self.property, client=self.crm_client, name="Плановый шаг", phone=self.crm_client.phone,
        )
        Lead.objects.filter(pk=planned.pk).update(
            first_contacted_at=now - timedelta(minutes=5), next_action_due_at=now + timedelta(hours=1),
        )
        archived = Lead.objects.create(
            property=self.property, client=self.crm_client, name="Архив", phone=self.crm_client.phone,
        )
        Lead.objects.filter(pk=archived.pk).update(status="archived")

        response = self.client.get(reverse("lead_list"), {"queue": "all"})
        self.assertEqual(
            [lead.pk for lead in response.context["leads"]],
            [self.lead.pk, overdue_action.pk, awaiting.pk, planned.pk, archived.pk],
        )
        self.assertContains(response, 'class="lead-table__row is-urgent"', count=2)

    def test_lead_search_matches_client_object_and_address_and_preserves_filters(self):
        second_property = Property.objects.create(owner=self.owner, title="Студия", address="Лесная улица")
        matched = Lead.objects.create(
            property=second_property, client=self.crm_client, name="Борис", phone="+79990000222",
        )
        other_owner = User.objects.create_user("another-lead-owner", password="password")
        hidden_property = Property.objects.create(owner=other_owner, title="Лесная улица")
        Lead.objects.create(property=hidden_property, name="Скрытый", phone="+79990000333")

        url = reverse("lead_list")
        response = self.client.get(url, {"q": "Лесная", "status": "new"})
        self.assertEqual([lead.pk for lead in response.context["leads"]], [matched.pk])
        self.assertEqual(response.context["status_tabs"][0]["url"], f"{url}?queue=all&q=%D0%9B%D0%B5%D1%81%D0%BD%D0%B0%D1%8F")
        self.assertIn("q=", response.context["pagination_query"])
        self.assertContains(response, 'name="q" value="Лесная"')
        cleared = self.client.get(url, {"q": "", "status": "new"}, HTTP_HX_REQUEST="true")
        self.assertEqual({lead.pk for lead in cleared.context["leads"]}, {self.lead.pk, matched.pk})
        self.assertContains(cleared, 'name="status" value="new"')
        htmx = self.client.get(url, {"q": "Анна"}, HTTP_HX_REQUEST="true")
        self.assertContains(htmx, 'id="lead-workspace"')
        self.assertContains(htmx, self.lead.name)

    def test_lead_and_client_show_latest_meaningful_contact_context(self):
        client_url = reverse("client_detail", args=[self.crm_client.pk])
        before = self.client.get(client_url)
        self.assertContains(before, "Перед звонком")
        self.assertContains(before, "Записать первый контакт")
        self.assertContains(before, "Изменить данные клиента")
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        lead_page = self.client.get(reverse("lead_detail", args=[self.lead.pk]))
        self.assertContains(lead_page, "Последний контакт")
        self.assertContains(lead_page, "Договорились о показе")
        client_page = self.client.get(client_url)
        self.assertContains(client_page, "Последнее обсуждение")
        self.assertContains(client_page, "Договорились о показе")
        self.assertNotContains(client_page, "Записать первый контакт")

        task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        self.client.post(reverse("lead_action_complete", args=[self.lead.pk]), self.completion_data(task))
        lead_page = self.client.get(reverse("lead_detail", args=[self.lead.pk]))
        self.assertContains(lead_page, "Последний итог")
        self.assertContains(lead_page, "Клиент подтвердил интерес")
        client_page = self.client.get(client_url)
        self.assertEqual(client_page.context["latest_client_discussion"]["kind"], "Итог действия")
        self.assertContains(client_page, "Клиент подтвердил интерес")

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

    def test_reopening_old_lead_sets_a_new_action_deadline(self):
        Lead.objects.filter(pk=self.lead.pk).update(next_action_due_at=timezone.now() - timedelta(days=1))
        url = reverse("lead_outcome", args=[self.lead.pk])
        self.client.post(url, {"action": "decline", "reason": "Клиент передумал"})
        self.client.post(url, {"action": "reopen"})
        self.lead.refresh_from_db()
        self.assertGreater(self.lead.next_action_due_at, timezone.now())
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).due_at, self.lead.next_action_due_at)

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

    def test_decline_requires_reason_and_reopen_restores_one_action(self):
        url = reverse("lead_outcome", args=[self.lead.pk])
        original_task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        response = self.client.post(url, {"action": "decline", "reason": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="lead-decline-panel" role="region" tabindex="-1" aria-labelledby="lead-decline-title" hidden data-lead-action-open-on-load')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.outcome, "open")

        self.assertRedirects(self.client.post(url, {"action": "decline", "reason": "Не подходит бюджет"}), reverse("lead_detail", args=[self.lead.pk]))
        self.client.post(url, {"action": "decline", "reason": "Повтор"})
        self.lead.refresh_from_db()
        original_task.refresh_from_db()
        self.assertEqual(self.lead.outcome, "declined")
        self.assertEqual(self.lead.outcome_reason, "Не подходит бюджет")
        self.assertIsNotNone(self.lead.outcome_at)
        self.assertTrue(original_task.is_done)
        self.assertIsNotNone(original_task.cancelled_at)
        self.assertFalse(ClientReminder.objects.filter(lead=self.lead, is_done=False).exists())
        self.assertEqual(ClientInteraction.objects.filter(lead=self.lead, text__contains="Не подходит бюджет").count(), 1)
        self.assertEqual(self.client.get(reverse("workday")).context["nav_new_leads_count"], 0)
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), "Не подходит бюджет")
        self.assertRedirects(self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data()), reverse("lead_detail", args=[self.lead.pk]))

        self.client.post(url, {"action": "reopen"})
        self.client.post(url, {"action": "reopen"})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.outcome, "open")
        self.assertEqual(self.lead.outcome_reason, "")
        self.assertIsNone(self.lead.outcome_at)
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead, is_done=False).count(), 1)
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead).count(), 2)

    def test_qualified_lead_can_join_existing_deal_without_duplicate(self):
        self.lead.qualification = "qualified"
        self.lead.first_contacted_at = timezone.now()
        self.lead.save(update_fields=["qualification", "first_contacted_at"])
        deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.crm_client, property=self.property)
        source_task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        detail = self.client.get(reverse("lead_detail", args=[self.lead.pk]))
        self.assertContains(detail, "Привязать к сделке")
        url = reverse("lead_outcome", args=[self.lead.pk])
        self.assertRedirects(self.client.post(url, {"action": "convert_existing", "deal_id": deal.pk}), reverse("lead_detail", args=[self.lead.pk]))
        self.client.post(url, {"action": "convert_existing", "deal_id": deal.pk})
        self.lead.refresh_from_db()
        source_task.refresh_from_db()
        self.assertEqual(self.lead.outcome_deal, deal)
        self.assertEqual(self.lead.outcome, "converted")
        self.assertTrue(source_task.is_done)
        self.assertEqual(ClientReminder.objects.filter(lead=self.lead, is_done=False).count(), 0)
        self.assertEqual(ClientReminder.objects.filter(deal=deal, is_done=False).count(), 1)
        self.assertEqual(ClientReminder.objects.get(deal=deal).text, source_task.text)
        self.assertEqual(Deal.objects.count(), 1)
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), "Переведена в сделку")

    def test_existing_deal_must_match_lead_workspace_and_pair(self):
        self.lead.qualification = "qualified"
        self.lead.first_contacted_at = timezone.now()
        self.lead.save(update_fields=["qualification", "first_contacted_at"])
        other = User.objects.create_user("other-deal-owner", password="password")
        foreign_client = Client.objects.create(owner=other, name="Иван", phone="+79990000333")
        foreign_property = Property.objects.create(owner=other, title="Чужой объект")
        foreign_deal = Deal.objects.create(owner=other, client=foreign_client, property=foreign_property)

        response = self.client.post(reverse("lead_outcome", args=[self.lead.pk]), {"action": "convert_existing", "deal_id": foreign_deal.pk})

        self.assertEqual(response.status_code, 404)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.outcome, "open")
        self.assertTrue(ClientReminder.objects.filter(lead=self.lead, is_done=False).exists())

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

    def test_workday_opens_first_contact_then_action_outcome_without_duplicate_task(self):
        received_at = timezone.localtime(timezone.now()).replace(hour=12, minute=0, second=0, microsecond=0)
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=received_at, next_action_due_at=received_at + timedelta(minutes=30),
        )
        with patch("properties.workday_views.timezone.now", return_value=received_at + timedelta(minutes=5)):
            response = self.client.get(reverse("workday"), {"item": f"lead:{self.lead.pk}"})
        self.assertContains(response, f"{reverse('lead_detail', args=[self.lead.pk])}?contact=1#lead-edit")
        self.assertContains(response, ">Записать контакт</a>")
        self.assertNotContains(response, reverse("task_detail", args=[ClientReminder.objects.get(lead=self.lead).pk]))
        detail = self.client.get(reverse("lead_detail", args=[self.lead.pk]), {"contact": "1"})
        self.assertContains(detail, 'id="lead-edit" data-lead-action-open="lead-edit-panel" aria-expanded="false" aria-controls="lead-edit-panel"')
        self.assertContains(detail, 'id="lead-edit-panel" role="region" tabindex="-1" aria-labelledby="lead-edit-title" hidden data-lead-action-open-on-load')
        self.assertContains(detail, 'id="lead-action" data-lead-action-open="lead-action-panel" aria-expanded="false" aria-controls="lead-action-panel"')

        Lead.objects.filter(pk=self.lead.pk).update(next_action_due_at=received_at - timedelta(minutes=5))
        with patch("properties.workday_views.timezone.now", return_value=received_at + timedelta(minutes=5)):
            response = self.client.get(reverse("workday"), {"item": f"lead:{self.lead.pk}"})
        self.assertContains(response, f"{reverse('lead_detail', args=[self.lead.pk])}?contact=1#lead-edit")
        self.assertContains(response, ">Записать контакт</a>")

        Lead.objects.filter(pk=self.lead.pk).update(first_contacted_at=received_at + timedelta(minutes=2))
        with patch("properties.workday_views.timezone.now", return_value=received_at + timedelta(minutes=5)):
            response = self.client.get(reverse("workday"), {"item": f"lead:{self.lead.pk}"})
        self.assertContains(response, f"{reverse('lead_detail', args=[self.lead.pk])}?complete=1#lead-action")
        self.assertContains(response, ">Записать итог</a>")
        detail = self.client.get(reverse("lead_detail", args=[self.lead.pk]), {"complete": "1"})
        self.assertContains(detail, 'id="lead-action" data-lead-action-open="lead-action-panel" aria-expanded="false" aria-controls="lead-action-panel"')
        self.assertContains(detail, 'id="lead-action-panel" role="region" tabindex="-1" aria-labelledby="lead-action-title" hidden data-lead-action-open-on-load')

    @override_settings(AI_ENABLED=True)
    def test_lead_card_shows_summary_and_side_drawer_actions(self):
        response = self.client.get(reverse("lead_detail", args=[self.lead.pk]))
        self.assertContains(response, "Следующее действие")
        self.assertContains(response, "Ответить клиенту до")
        self.assertContains(response, 'id="lead-edit" data-lead-action-open="lead-edit-panel" aria-expanded="false" aria-controls="lead-edit-panel"')
        self.assertContains(response, 'id="lead-action" data-lead-action-open="lead-action-panel" aria-expanded="false" aria-controls="lead-action-panel"')
        self.assertContains(response, '<span>Записать контакт</span>')
        self.assertContains(response, '<span>Записать итог</span>')
        self.assertContains(response, f'href="tel:{self.lead.phone}" aria-label="Позвонить {self.lead.phone}"')
        self.assertContains(response, '<use href="#ui-icon-phone"></use>')
        self.assertContains(response, '<use href="#ui-icon-edit"></use>')
        self.assertContains(response, 'class="lead-contact__call-label">Позвонить</span>')
        self.assertContains(response, '<use href="#ui-icon-check"></use>')
        self.assertContains(response, 'class="detail-main-stack lead-detail-main"')
        self.assertContains(response, 'class="lead-overview__outcome-action"')
        self.assertContains(response, "Закрыть без сделки")
        self.assertNotContains(response, 'id="lead-ai-reply"')
        self.assertContains(response, 'id="lead-edit-panel" role="region" tabindex="-1" aria-labelledby="lead-edit-title" hidden>')
        self.assertContains(response, 'id="lead-action-panel" role="region" tabindex="-1" aria-labelledby="lead-action-title" hidden>')
        self.assertContains(response, 'aria-label="Пояснение: Следующее действие"')
        self.assertContains(response, 'id="id_lead_next_action"')
        self.assertContains(response, 'id="id_complete_next_action"')
        html = response.content.decode()
        self.assertLess(html.index('id="lead-overview" role="tabpanel"'), html.index('id="lead-about" role="tabpanel"'))
        self.assertLess(html.index('id="lead-about" role="tabpanel"'), html.index('id="lead-history" role="tabpanel"'))
        self.assertLess(html.index('name="action" value="decline"'), html.index('id="lead-about" role="tabpanel"'))
        self.assertEqual(html.count('name="action" value="decline"'), 1)

    def test_contacted_lead_distinguishes_plan_edit_from_action_outcome(self):
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        response = self.client.get(reverse("lead_detail", args=[self.lead.pk]))

        self.assertContains(response, '<span>Изменить план</span>')
        self.assertContains(response, '<label class="form-label" for="id_lead_next_action">Текущее действие</label>')
        self.assertContains(response, 'value="save" class="btn btn-primary">Сохранить план</button>')
        self.assertContains(response, '<span>Записать итог</span>')
        self.assertContains(response, '<label class="form-label" for="id_complete_next_action">Следующее действие</label>')
        self.assertNotContains(response, "Изменить первый контакт и следующий шаг")

    def test_lead_list_keeps_contacted_and_archived_leads_visible(self):
        self.lead.status = "read"
        self.lead.save(update_fields=["status"])
        response = self.client.get(reverse("lead_list"))
        self.assertEqual(list(response.context["leads"]), [self.lead])
        self.assertNotContains(response, 'aria-label="Очереди заявок"')
        self.client.post(reverse("lead_process", args=[self.lead.pk]), self.processing_data())
        response = self.client.get(reverse("lead_list"), {"queue": "reply"})
        self.assertEqual(list(response.context["leads"]), [self.lead])
        self.lead.status = "archived"
        self.lead.save(update_fields=["status"])
        response = self.client.get(reverse("lead_list"), {"queue": "overdue"})
        self.assertEqual(list(response.context["leads"]), [self.lead])
        response = self.client.get(reverse("lead_list"), {"status": "new"})
        self.assertEqual(list(response.context["leads"]), [])

    def test_list_highlights_overdue_first_response_without_marking_future_action_overdue(self):
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=now - timedelta(hours=1), next_action_due_at=now + timedelta(hours=2),
        )
        response = self.client.get(reverse("lead_list"))
        self.assertContains(response, "Первый ответ просрочен")
        self.assertContains(response, "Задача до")
        self.assertEqual(response.content.decode().count("lead-deadline--overdue"), 2)

    def test_list_shows_one_deadline_when_first_response_and_task_coincide(self):
        self.lead.refresh_from_db()
        Lead.objects.filter(pk=self.lead.pk).update(next_action_due_at=self.lead.first_response_due_at)
        response = self.client.get(reverse("lead_list"))
        html = response.content.decode()
        self.assertEqual(html.count("Первый ответ до"), 2)
        self.assertNotIn('class="lead-action-summary__secondary', html)
        mobile_card = html.split('<div class="mobile-records lead-mobile-list">', 1)[1].split("</article>", 1)[0]
        self.assertNotIn("Подробнее", mobile_card)
        self.assertIn('class="lead-mobile-card__meta"', mobile_card)

        Lead.objects.filter(pk=self.lead.pk).update(next_action="Отправить презентацию")
        response = self.client.get(reverse("lead_list"))
        self.assertContains(response, "Отправить презентацию")
        self.assertNotContains(response, 'class="lead-action-summary__secondary')

    def test_list_puts_earlier_task_before_first_response(self):
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=now - timedelta(minutes=10), next_action_due_at=now + timedelta(minutes=5),
        )
        response = self.client.get(reverse("lead_list"))
        self.assertContains(response, "Задача до")
        self.assertContains(response, "Первый ответ до")
        self.assertEqual(response.content.decode().count('class="lead-action-summary__secondary'), 2)

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

    def test_overdue_lead_list_and_today_show_assigned_next_step(self):
        Lead.objects.filter(pk=self.lead.pk).update(next_action_due_at=timezone.now() - timedelta(minutes=1))
        lead_list = self.client.get(reverse("lead_list"))
        self.assertEqual(list(lead_list.context["leads"]), [self.lead])
        self.assertContains(lead_list, "Задача просрочена")
        today = self.client.get(reverse("workday"))
        self.assertContains(today, "Квартира у парка")
        self.assertEqual(today.context["nav_overdue_count"], 1)

    def test_rescheduling_next_action_does_not_hide_missed_first_response(self):
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=timezone.now() - timedelta(minutes=31),
            next_action_due_at=timezone.now() + timedelta(days=1),
        )
        lead_list = self.client.get(reverse("lead_list"))
        self.assertEqual([lead.pk for lead in lead_list.context["leads"]], [self.lead.pk])
        self.assertContains(lead_list, "Первый ответ просрочен")
        detail = self.client.get(reverse("lead_detail", args=[self.lead.pk]))
        self.assertContains(detail, "Первый ответ просрочен")
        self.assertEqual(self.client.get(reverse("workday")).context["nav_overdue_count"], 1)

    def test_detail_shows_one_deadline_when_times_share_a_displayed_minute(self):
        minute = timezone.localtime(timezone.now()).replace(second=0, microsecond=0) + timedelta(minutes=10)
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=minute - timedelta(minutes=30) + timedelta(seconds=5),
            next_action_due_at=minute + timedelta(seconds=40),
        )
        url = reverse("lead_detail", args=[self.lead.pk])
        response = self.client.get(url)
        self.assertTrue(response.context["lead_deadlines_match"])
        self.assertNotContains(response, 'class="lead-next__secondary-deadline')
        lead_list = self.client.get(reverse("lead_list"))
        self.assertContains(lead_list, "Первый ответ до")
        self.assertNotContains(lead_list, 'class="lead-action-summary__secondary')

        Lead.objects.filter(pk=self.lead.pk).update(next_action_due_at=minute + timedelta(hours=1))
        response = self.client.get(url)
        self.assertFalse(response.context["lead_deadlines_match"])
        self.assertContains(response, 'class="lead-next__secondary-deadline')
        self.assertContains(self.client.get(reverse("lead_list")), 'class="lead-action-summary__secondary')

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


class LeadOutcomeMigrationTests(TransactionTestCase):
    def test_existing_linked_deal_closes_lead_without_creating_another_task(self):
        old_target = [("properties", "0041_clientreminder_cancelled_at_clientreminder_lead_and_more")]
        new_target = [("properties", "0042_lead_outcome_lead_outcome_at_lead_outcome_deal_and_more")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        OldUser = old_apps.get_model("auth", "User")
        OldClient = old_apps.get_model("properties", "Client")
        OldProperty = old_apps.get_model("properties", "Property")
        OldLead = old_apps.get_model("properties", "Lead")
        OldDeal = old_apps.get_model("properties", "Deal")
        OldTask = old_apps.get_model("properties", "ClientReminder")
        user = OldUser.objects.create(username="old-lead-owner")
        client = OldClient.objects.create(owner_id=user.pk, name="Анна", phone="+79990000444")
        property = OldProperty.objects.create(owner_id=user.pk, title="Квартира")
        lead = OldLead.objects.create(property_id=property.pk, client_id=client.pk, assigned_to_id=user.pk, name="Анна", phone=client.phone)
        deal = OldDeal.objects.create(owner_id=user.pk, client_id=client.pk, property_id=property.pk, lead_id=lead.pk)
        task = OldTask.objects.create(owner_id=user.pk, client_id=client.pk, lead_id=lead.pk, text="Позвонить", due_at=timezone.now() + timedelta(days=1))

        new_executor = MigrationExecutor(connection)
        new_executor.migrate(new_target)
        new_apps = new_executor.loader.project_state(new_target).apps
        NewLead = new_apps.get_model("properties", "Lead")
        NewTask = new_apps.get_model("properties", "ClientReminder")
        migrated_lead = NewLead.objects.get(pk=lead.pk)
        migrated_task = NewTask.objects.get(pk=task.pk)
        self.assertEqual(migrated_lead.outcome, "converted")
        self.assertEqual(migrated_lead.outcome_deal_id, deal.pk)
        self.assertIsNotNone(migrated_lead.outcome_at)
        self.assertTrue(migrated_task.is_done)
        self.assertIsNotNone(migrated_task.cancelled_at)
        self.assertFalse(NewTask.objects.filter(deal_id=deal.pk).exists())
