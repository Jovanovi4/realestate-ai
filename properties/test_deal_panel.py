from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientReminder, Deal, Property, Showing


class DealPanelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("deal-panel-owner", password="password")
        self.client_record = Client.objects.create(
            owner=self.user, name="Анна", phone="+79990000001",
        )
        self.property = Property.objects.create(owner=self.user, title="Квартира у парка")
        self.deal = Deal.objects.create(
            owner=self.user, responsible=self.user, client=self.client_record, property=self.property,
        )
        self.board_url = reverse("deal_board")
        self.client.force_login(self.user)

    def test_direct_link_opens_compact_panel_and_marks_the_board_card(self):
        response = self.client.get(self.board_url, {"deal": self.deal.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="deal-panel"')
        self.assertContains(response, 'id="deal-panel-slot" class="deal-panel-slot is-open"')
        self.assertContains(response, 'data-deal-panel-open="1"')
        self.assertContains(response, 'deal-board__card is-open')
        self.assertContains(response, 'name="expected_stage" value="new"')
        self.assertContains(response, 'name="stage"')
        self.assertContains(response, 'class="badge deal-stage-badge is-new"')
        self.assertContains(response, 'class="detail-status-control__editor deal-panel__stage-editor"')
        self.assertContains(response, 'data-panel-status-save')
        self.assertContains(response, 'data-deal-panel-loss-field hidden')
        self.assertContains(response, reverse("deal_detail", args=[self.deal.pk]))
        self.assertContains(response, "Квартира у парка")
        self.assertContains(response, "Запланируйте действие")
        self.assertContains(response, 'class="lead-panel__actions deal-panel__quick-actions"')
        self.assertContains(response, f'href="{reverse("task_create")}?deal={self.deal.pk}"')
        self.assertContains(response, f'href="{reverse("showing_create")}?deal={self.deal.pk}"')
        self.assertEqual(response.content.decode().count("Добавить задачу"), 1)

    def test_fragment_open_close_and_board_paging_keep_page_context(self):
        response = self.client.get(self.board_url, {"p_new": "2", "deal": self.deal.pk}, HTTP_X_DEAL_PANEL="1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="deal-panel-slot"')
        self.assertContains(response, 'href="?p_new=2#deal-board-new" data-deal-panel-close')
        self.assertNotContains(response, 'data-deal-board')
        self.assertNotContains(response, '<h1 class="ui-page-header__title">Сделки</h1>')

        closed = self.client.get(self.board_url, HTTP_X_DEAL_PANEL="1")
        self.assertContains(closed, 'id="deal-panel-slot" class="deal-panel-slot"')
        self.assertNotContains(closed, 'id="deal-panel"')

    def test_next_deal_stays_in_the_same_stage(self):
        another_client = Client.objects.create(
            owner=self.user, name="Борис", phone="+79990000002",
        )
        next_deal = Deal.objects.create(
            owner=self.user, responsible=self.user, client=another_client, property=self.property,
        )
        panel = self.client.get(self.board_url, {"deal": self.deal.pk}, HTTP_X_DEAL_PANEL="1")

        self.assertContains(panel, f'data-deal-panel-open="{next_deal.pk}"')
        self.assertContains(panel, 'aria-label="Открыть следующую сделку на этом этапе"')

        next_deal.stage = "viewing"
        next_deal.save(update_fields=["stage"])
        panel = self.client.get(self.board_url, {"deal": self.deal.pk}, HTTP_X_DEAL_PANEL="1")
        self.assertNotContains(panel, 'aria-label="Открыть следующую сделку на этом этапе"')

    def test_stage_change_refreshes_panel_and_board_without_changing_other_details(self):
        stage_url = reverse("deal_stage_update", args=[self.deal.pk])
        denied = self.client.post(stage_url, {"stage": "lost", "expected_stage": "new"})
        self.assertEqual(denied.status_code, 400)

        changed = self.client.post(stage_url, {
            "stage": "lost", "expected_stage": "new", "outcome_note": "Клиент выбрал другой объект",
        })
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json(), {"stage": "lost", "changed": True})
        panel = self.client.get(self.board_url, {"deal": self.deal.pk}, HTTP_X_DEAL_PANEL="1")
        self.assertContains(panel, 'class="badge deal-stage-badge is-lost"')
        self.assertContains(panel, "Клиент выбрал другой объект")
        board = self.client.get(self.board_url, {"deal": self.deal.pk})
        self.assertContains(board, 'data-deal-stage="lost" data-stage-url=')
        self.deal.refresh_from_db()
        self.assertEqual((self.deal.client, self.deal.property, self.deal.responsible), (
            self.client_record, self.property, self.user,
        ))

    def test_nearest_action_prefers_an_earlier_showing_then_warns_of_overdue_task(self):
        now = timezone.now()
        task = ClientReminder.objects.create(
            owner=self.user, deal=self.deal, text="Подготовить документы", due_at=now + timedelta(days=2),
        )
        showing = Showing.objects.create(
            owner=self.user, deal=self.deal, starts_at=now + timedelta(days=1),
        )
        first = self.client.get(self.board_url, {"deal": self.deal.pk}, HTTP_X_DEAL_PANEL="1")
        self.assertContains(first, (
            f'<strong><a class="deal-panel__focus-link" href="{reverse("showing_detail", args=[showing.pk])}">'
            'Показ объекта</a></strong>'
        ))
        self.assertContains(first, 'class="lead-panel__focus deal-panel__focus-card"')
        self.assertNotContains(first, "Открыть показ")

        showing.status = "completed"
        showing.outcome_note = "Показ прошёл"
        showing.save(update_fields=["status", "outcome_note"])
        task.due_at = now - timedelta(hours=1)
        task.save(update_fields=["due_at"])
        second = self.client.get(self.board_url, {"deal": self.deal.pk}, HTTP_X_DEAL_PANEL="1")
        self.assertContains(second, "Подготовить документы")
        self.assertContains(second, "Просрочено ·")
        self.assertContains(second, reverse("task_detail", args=[task.pk]))

    def test_private_deal_cannot_be_opened_from_another_account(self):
        stranger = User.objects.create_user("deal-panel-stranger", password="password")
        self.client.force_login(stranger)

        self.assertEqual(self.client.get(self.board_url, {"deal": self.deal.pk}).status_code, 404)
        self.assertEqual(self.client.get(
            self.board_url, {"deal": self.deal.pk}, HTTP_X_DEAL_PANEL="1",
        ).status_code, 404)
        self.assertEqual(self.client.get(self.board_url, {"deal": "invalid"}).status_code, 404)

    def test_team_member_can_open_shared_deal_but_not_personal_deal(self):
        colleague = User.objects.create_user("deal-panel-colleague", password="password")
        agency = Agency.objects.create(owner=self.user, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        AgencyMembership.objects.create(agency=agency, user=colleague, role="agent")
        team_client = Client.objects.create(owner=self.user, agency=agency, name="Мария", phone="+79990000003")
        team_property = Property.objects.create(owner=self.user, agency=agency, title="Общий объект")
        shared_deal = Deal.objects.create(
            owner=self.user, agency=agency, responsible=colleague,
            client=team_client, property=team_property,
        )
        showing = Showing.objects.create(
            owner=self.user, agency=agency, deal=shared_deal,
            starts_at=timezone.now() + timedelta(days=1),
        )

        self.client.force_login(colleague)
        panel = self.client.get(self.board_url, {"deal": shared_deal.pk}, HTTP_X_DEAL_PANEL="1")

        self.assertContains(panel, "Мария")
        self.assertContains(panel, "Общий объект")
        self.assertContains(panel, "<strong>Показ объекта</strong>")
        self.assertNotContains(panel, 'deal-panel__focus-card')
        self.assertNotContains(panel, reverse("showing_detail", args=[showing.pk]))
        self.assertEqual(self.client.get(self.board_url, {"deal": self.deal.pk}).status_code, 404)
