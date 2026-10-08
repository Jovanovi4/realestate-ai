from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientInteraction, Deal, Property


class ClientRelationshipTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("relationship-owner", password="password")
        self.client_record = Client.objects.create(owner=self.owner, name="Анна", phone="+79991112233")
        self.property = Property.objects.create(owner=self.owner, title="Квартира")
        self.client.force_login(self.owner)

    def test_deal_stages_and_client_relationship_are_independent(self):
        deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property)
        board = self.client.get(reverse("client_board"))
        self.assertEqual([column["key"] for column in board.context["columns"]], ["new", "in_progress", "nurture", "inactive"])
        self.assertContains(board, "Сделок в работе: 1")

        blocked = self.client.post(
            reverse("client_status_update", args=[self.client_record.pk]),
            {"status": "inactive"}, HTTP_HX_REQUEST="true",
        )
        self.assertContains(blocked, "активная сделка")
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.status, "new")
        self.assertEqual(deal.stage, "new")

        deal.stage = "lost"
        deal.save()
        self.client.post(reverse("client_status_update", args=[self.client_record.pk]), {"status": "inactive"})
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.status, "inactive")
        self.assertEqual(deal.stage, "lost")
        self.assertTrue(ClientInteraction.objects.filter(client=self.client_record, text__contains="Неактуален").exists())

        second_property = Property.objects.create(owner=self.owner, title="Дом")
        response = self.client.post(reverse("deal_create"), {
            "client": self.client_record.pk, "property": second_property.pk, "lead": "",
            "responsible": self.owner.pk, "stage": "new", "expected_commission": "", "outcome_note": "",
        })
        self.assertEqual(response.status_code, 302)
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.status, "in_progress")
        self.assertEqual(list(self.client_record.deals.order_by("pk").values_list("stage", flat=True)), ["lost", "new"])

    def test_client_form_uses_relationship_choices(self):
        response = self.client.get(reverse("client_create"))
        self.assertContains(response, "На связи")
        self.assertContains(response, "Неактуален")
        self.assertNotContains(response, '<option value="viewing"')
        self.assertNotContains(response, '<option value="won"')

    def test_old_deal_stage_cannot_be_set_as_client_relationship(self):
        response = self.client.post(
            reverse("client_status_update", args=[self.client_record.pk]),
            {"status": "viewing"}, HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, "Показ и итог по объекту меняйте в сделке")
        self.assertContains(response, 'class="detail-status-control__editor" x-data="{ changed: false }" open')
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.status, "new")

    def test_bulk_relationship_change_updates_selected_clients_and_history_once(self):
        second = Client.objects.create(owner=self.owner, name="Борис", phone="+79991112234")
        response = self.client.get(reverse("client_list"))
        self.assertContains(response, 'id="client-bulk-status"')
        self.assertContains(response, 'name="action" value="status"')

        payload = {
            "action": "status", "status": "nurture",
            "client_ids": [self.client_record.pk, self.client_record.pk, second.pk],
            "next": f"{reverse('client_list')}?status=new",
        }
        response = self.client.post(reverse("client_bulk_action"), payload)
        self.assertRedirects(response, payload["next"])
        self.client_record.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((self.client_record.status, second.status), ("nurture", "nurture"))
        self.assertEqual(ClientInteraction.objects.filter(interaction_type="status", client__in=[self.client_record, second]).count(), 2)

        self.client.post(reverse("client_bulk_action"), payload)
        self.assertEqual(ClientInteraction.objects.filter(interaction_type="status", client__in=[self.client_record, second]).count(), 2)

    def test_bulk_relationship_change_is_atomic_when_one_client_has_active_deal(self):
        blocked = Client.objects.create(owner=self.owner, name="Борис", phone="+79991112234")
        Deal.objects.create(owner=self.owner, responsible=self.owner, client=blocked, property=self.property)

        response = self.client.post(reverse("client_bulk_action"), {
            "action": "status", "status": "inactive", "client_ids": [self.client_record.pk, blocked.pk],
        }, follow=True)

        self.assertContains(response, "Борис: У клиента есть активная сделка")
        self.client_record.refresh_from_db()
        blocked.refresh_from_db()
        self.assertEqual((self.client_record.status, blocked.status), ("new", "new"))
        self.assertFalse(ClientInteraction.objects.filter(interaction_type="status", client__in=[self.client_record, blocked]).exists())

    def test_bulk_relationship_change_rejects_clients_outside_workspace(self):
        other = User.objects.create_user("other-owner", password="password")
        outside = Client.objects.create(owner=other, name="Чужой клиент", phone="+79991112235")

        response = self.client.post(reverse("client_bulk_action"), {
            "action": "status", "status": "nurture", "client_ids": [self.client_record.pk, outside.pk],
        }, follow=True)

        self.assertContains(response, "Некоторые выбранные клиенты недоступны")
        self.client_record.refresh_from_db()
        outside.refresh_from_db()
        self.assertEqual((self.client_record.status, outside.status), ("new", "new"))

    def test_team_agent_can_change_relationships_without_delete_permission(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        agent = User.objects.create_user("relationship-agent", password="password")
        AgencyMembership.objects.create(agency=agency, user=agent, role="agent")
        shared = Client.objects.create(owner=self.owner, agency=agency, name="Общий клиент", phone="+79991112236")
        self.client.force_login(agent)

        response = self.client.get(reverse("client_list"))
        self.assertFalse(response.context["can_bulk_delete"])
        self.assertContains(response, f'name="client_ids" value="{shared.pk}"')
        self.assertNotContains(response, 'name="action" value="delete"')

        self.client.post(reverse("client_bulk_action"), {
            "action": "status", "status": "nurture", "client_ids": [shared.pk],
        })
        shared.refresh_from_db()
        self.assertEqual(shared.status, "nurture")


class ClientRelationshipMigrationTests(TransactionTestCase):
    def test_old_client_stages_are_mapped_without_changing_deals_and_can_be_restored(self):
        previous_target = [("properties", "0044_client_search_currency_client_search_deal_type_and_more")]
        current_target = [("properties", "0045_client_legacy_status_alter_client_outcome_reason_and_more")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(previous_target)
        old_apps = executor.loader.project_state(previous_target).apps
        OldUser = old_apps.get_model("auth", "User")
        OldClient = old_apps.get_model("properties", "Client")
        OldProperty = old_apps.get_model("properties", "Property")
        OldDeal = old_apps.get_model("properties", "Deal")
        OldLead = old_apps.get_model("properties", "Lead")
        user = OldUser.objects.create(username="relationship-migration-owner")
        apartment = OldProperty.objects.create(owner_id=user.pk, title="Квартира")
        house = OldProperty.objects.create(owner_id=user.pk, title="Дом")
        previous = {}
        for index, stage in enumerate(("new", "in_progress", "viewing", "negotiation", "won", "lost")):
            previous[stage] = OldClient.objects.create(
                owner_id=user.pk, name=f"Клиент {index}", phone=f"+7999111300{index}",
                status=stage, outcome_reason="Старый итог" if stage == "won" else "",
            )
        active_lost = OldClient.objects.create(owner_id=user.pk, name="Повторный клиент", phone="+79991113009", status="lost")
        contacted_new = OldClient.objects.create(
            owner_id=user.pk, name="Уже связались", phone="+79991113010", status="new", first_contacted_at=timezone.now(),
        )
        open_lost = OldClient.objects.create(owner_id=user.pk, name="Новое обращение", phone="+79991113011", status="lost")
        OldLead.objects.create(property_id=apartment.pk, client_id=open_lost.pk, name="Новое обращение", phone=open_lost.phone)
        OldDeal.objects.create(owner_id=user.pk, client_id=previous["negotiation"].pk, property_id=apartment.pk, stage="viewing")
        OldDeal.objects.create(owner_id=user.pk, client_id=previous["negotiation"].pk, property_id=house.pk, stage="lost")
        OldDeal.objects.create(owner_id=user.pk, client_id=active_lost.pk, property_id=house.pk, stage="in_progress")
        OldDeal.objects.create(owner_id=user.pk, client_id=previous["new"].pk, property_id=house.pk, stage="new")

        MigrationExecutor(connection).migrate(current_target)
        migrated_apps = MigrationExecutor(connection).loader.project_state(current_target).apps
        MigratedClient = migrated_apps.get_model("properties", "Client")
        MigratedDeal = migrated_apps.get_model("properties", "Deal")
        expected = {
            "new": ("in_progress", "new"),
            "in_progress": ("in_progress", ""),
            "viewing": ("in_progress", "viewing"),
            "negotiation": ("in_progress", "negotiation"),
            "won": ("nurture", "won"),
            "lost": ("inactive", "lost"),
        }
        for old_status, (relationship, legacy) in expected.items():
            client = MigratedClient.objects.get(pk=previous[old_status].pk)
            self.assertEqual((client.status, client.legacy_status), (relationship, legacy))
        self.assertEqual((MigratedClient.objects.get(pk=active_lost.pk).status, MigratedClient.objects.get(pk=active_lost.pk).legacy_status), ("in_progress", "lost"))
        self.assertEqual((MigratedClient.objects.get(pk=contacted_new.pk).status, MigratedClient.objects.get(pk=contacted_new.pk).legacy_status), ("in_progress", "new"))
        self.assertEqual((MigratedClient.objects.get(pk=open_lost.pk).status, MigratedClient.objects.get(pk=open_lost.pk).legacy_status), ("in_progress", "lost"))
        self.assertEqual(MigratedClient.objects.get(pk=previous["won"].pk).outcome_reason, "Старый итог")
        self.assertEqual(list(MigratedDeal.objects.filter(client_id=previous["negotiation"].pk).order_by("pk").values_list("stage", flat=True)), ["viewing", "lost"])

        MigrationExecutor(connection).migrate(previous_target)
        restored_apps = MigrationExecutor(connection).loader.project_state(previous_target).apps
        RestoredClient = restored_apps.get_model("properties", "Client")
        for old_status, client in previous.items():
            self.assertEqual(RestoredClient.objects.get(pk=client.pk).status, old_status)
        self.assertEqual(RestoredClient.objects.get(pk=active_lost.pk).status, "lost")
        self.assertEqual(RestoredClient.objects.get(pk=contacted_new.pk).status, "new")
        self.assertEqual(RestoredClient.objects.get(pk=open_lost.pk).status, "lost")
