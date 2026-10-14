from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from billing.enforcement import TariffDenied

from .models import Agency, AgencyMembership, Client, ClientInteraction, Deal, Property


class DealBoardStageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("board-stage-owner", password="password")
        self.other = User.objects.create_user("board-stage-other", password="password")
        self.client_record = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000001")
        self.property = Property.objects.create(owner=self.owner, title="Квартира у парка")
        self.deal = Deal.objects.create(
            owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property,
        )
        self.url = reverse("deal_stage_update", args=[self.deal.pk])
        self.client.force_login(self.owner)

    def move(self, stage, *, expected="new", note=""):
        return self.client.post(self.url, {
            "stage": stage, "expected_stage": expected, "outcome_note": note,
        })

    def test_board_uses_card_drag_and_keeps_stage_editing_in_deal(self):
        response = self.client.get(reverse("deal_board"))

        self.assertContains(response, 'data-deal-board')
        self.assertContains(response, 'draggable="true"')
        self.assertContains(response, f'data-stage-url="{self.url}"')
        self.assertNotContains(response, 'data-deal-stage-open')
        self.assertNotContains(response, 'data-deal-stage-select')
        self.assertContains(response, 'data-deal-loss-note')
        self.assertContains(response, reverse("deal_detail", args=[self.deal.pk]))
        self.assertContains(self.client.get(reverse("deal_detail", args=[self.deal.pk])), 'id="id_stage"')

    def test_closed_deals_are_prominent_when_no_active_deals_remain(self):
        self.assertEqual(self.move("won").status_code, 200)
        response = self.client.get(reverse("deal_board"))

        self.assertEqual(response.context["active_count"], 0)
        self.assertEqual(response.context["closed_count"], 1)
        self.assertContains(response, "Активных сделок нет")
        self.assertContains(response, "deal-board--closed-focus")
        self.assertContains(response, '<p class="deal-board__closed-heading">Завершённые сделки</p>')
        self.assertContains(response, f'data-deal-id="{self.deal.pk}" data-deal-stage="won"')

        filtered = self.client.get(reverse("deal_board"), {"stage": "won"})
        self.assertNotContains(filtered, "deal-board--closed-focus")

    def test_move_updates_stage_and_history_without_changing_other_fields(self):
        response = self.move("viewing")

        self.assertEqual((response.status_code, response.json()), (200, {"stage": "viewing", "changed": True}))
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, "viewing")
        self.assertEqual((self.deal.client, self.deal.property, self.deal.responsible), (
            self.client_record, self.property, self.owner,
        ))
        self.assertIsNone(self.deal.closed_at)
        self.assertTrue(ClientInteraction.objects.filter(
            client=self.client_record, interaction_type="status", text__contains="Новая → Показ",
        ).exists())
        board = self.client.get(reverse("deal_board"))
        self.assertEqual(next(column["count"] for column in board.context["columns"] if column["key"] == "viewing"), 1)

    def test_refusal_requires_reason_and_reopening_restores_active_relationship(self):
        missing = self.move("lost")
        self.assertEqual(missing.status_code, 400)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, "new")

        refused = self.move("lost", note="Клиент выбрал другой объект")
        self.assertEqual(refused.status_code, 200)
        self.deal.refresh_from_db()
        self.assertEqual((self.deal.stage, self.deal.outcome_note), ("lost", "Клиент выбрал другой объект"))
        self.assertIsNotNone(self.deal.closed_at)

        self.client_record.status = "inactive"
        self.client_record.save(update_fields=["status"])
        reopened = self.move("in_progress", expected="lost")
        self.assertEqual(reopened.status_code, 200)
        self.deal.refresh_from_db()
        self.client_record.refresh_from_db()
        self.assertIsNone(self.deal.closed_at)
        self.assertEqual(self.client_record.status, "in_progress")

    def test_stale_or_invalid_move_does_not_change_deal(self):
        self.assertEqual(self.move("unknown").status_code, 400)
        self.assertEqual(self.move("viewing", expected="negotiation").status_code, 409)
        self.assertEqual(self.move("new").json(), {"stage": "new", "changed": False})
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, "new")
        self.assertFalse(ClientInteraction.objects.exists())

    def test_reopening_cannot_create_two_active_deals_for_one_pair(self):
        self.assertEqual(self.move("lost", note="Не договорились о цене").status_code, 200)
        Deal.objects.create(
            owner=self.owner, responsible=self.owner,
            client=self.client_record, property=self.property,
        )

        response = self.move("viewing", expected="lost")

        self.assertEqual(response.status_code, 409)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, "lost")
        self.assertIsNotNone(self.deal.closed_at)

    def test_workspace_and_tariff_checks_apply_to_stage_move(self):
        self.client.force_login(self.other)
        self.assertEqual(self.move("viewing").status_code, 404)
        self.assertEqual(self.move("lost").status_code, 404)
        self.client.force_login(self.owner)
        denial = TariffDenied(SimpleNamespace(code="denied", message="Действие недоступно по тарифу"))
        with patch("properties.deal_views.require_resource_action", side_effect=denial):
            response = self.move("viewing")
        self.assertEqual(response.status_code, 403)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, "new")

    def test_team_member_can_move_shared_deal_but_not_private_deal(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="agent")
        shared_client = Client.objects.create(owner=self.owner, agency=agency, name="Мария", phone="+79990000002")
        shared_property = Property.objects.create(owner=self.owner, agency=agency, title="Общий объект")
        shared_deal = Deal.objects.create(
            owner=self.owner, agency=agency, responsible=self.other,
            client=shared_client, property=shared_property,
        )
        self.client.force_login(self.other)

        response = self.client.post(reverse("deal_stage_update", args=[shared_deal.pk]), {
            "stage": "negotiation", "expected_stage": "new",
        })

        self.assertEqual(response.status_code, 200)
        shared_deal.refresh_from_db()
        self.assertEqual(shared_deal.stage, "negotiation")
        self.assertEqual(self.move("viewing").status_code, 404)
