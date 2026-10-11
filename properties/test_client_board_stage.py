from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from billing.enforcement import TariffDenied

from .models import Agency, AgencyMembership, Client, ClientInteraction, Deal, Property


class ClientBoardStageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("client-board-owner", password="password")
        self.other = User.objects.create_user("client-board-other", password="password")
        self.record = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000001")
        self.url = reverse("client_board_status_update", args=[self.record.pk])
        self.client.force_login(self.owner)

    def move(self, status, *, expected="new"):
        return self.client.post(self.url, {"status": status, "expected_status": expected})

    def test_board_cards_can_be_dragged_and_detail_link_is_kept(self):
        response = self.client.get(reverse("client_board"))

        self.assertContains(response, 'data-client-board')
        self.assertContains(response, f'data-status-url="{self.url}"')
        self.assertContains(response, 'draggable="true"')
        self.assertContains(response, reverse("client_detail", args=[self.record.pk]))

    def test_move_persists_in_board_and_records_history_once(self):
        response = self.move("in_progress")

        self.assertEqual((response.status_code, response.json()), (200, {"status": "in_progress", "changed": True}))
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "in_progress")
        self.assertEqual(ClientInteraction.objects.filter(client=self.record, interaction_type="status").count(), 1)
        board = self.client.get(reverse("client_board"))
        self.assertEqual([(column["key"], column["count"]) for column in board.context["columns"]], [
            ("new", 0), ("in_progress", 1), ("nurture", 0), ("inactive", 0),
        ])
        self.assertContains(board, 'data-client-status="in_progress"')

    def test_stale_invalid_and_unchanged_moves_leave_relationship_alone(self):
        self.assertEqual(self.move("viewing").status_code, 400)
        self.assertEqual(self.move("nurture", expected="inactive").status_code, 409)
        self.assertEqual(self.move("new").json(), {"status": "new", "changed": False})
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "new")
        self.assertFalse(ClientInteraction.objects.filter(client=self.record).exists())

    def test_move_obeys_first_contact_and_active_deal_rules(self):
        self.record.first_contacted_at = timezone.now()
        self.record.save(update_fields=["first_contacted_at"])
        self.assertEqual(self.move("in_progress").status_code, 200)
        self.assertEqual(self.move("new", expected="in_progress").status_code, 409)

        property = Property.objects.create(owner=self.owner, title="Квартира")
        Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.record, property=property)
        blocked = self.move("inactive", expected="in_progress")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("активная сделка", blocked.json()["error"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "in_progress")
        self.assertEqual(ClientInteraction.objects.filter(client=self.record, interaction_type="status").count(), 1)

    def test_workspace_and_tariff_checks_apply_to_board_moves(self):
        self.client.force_login(self.other)
        self.assertEqual(self.move("nurture").status_code, 404)

        self.client.force_login(self.owner)
        denial = TariffDenied(SimpleNamespace(code="denied", message="Действие недоступно по тарифу"))
        with patch("properties.views.require_resource_action", side_effect=denial):
            response = self.move("nurture")
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "new")

    def test_team_member_can_move_shared_client_but_not_private_client(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="agent")
        shared = Client.objects.create(owner=self.owner, agency=agency, name="Мария", phone="+79990000002")
        self.client.force_login(self.other)

        response = self.client.post(reverse("client_board_status_update", args=[shared.pk]), {
            "status": "nurture", "expected_status": "new",
        })

        self.assertEqual(response.status_code, 200)
        shared.refresh_from_db()
        self.assertEqual(shared.status, "nurture")
        self.assertEqual(self.move("nurture").status_code, 404)
