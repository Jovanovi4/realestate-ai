from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ClientSearchForm
from .models import Agency, AgencyMembership, Client, ClientInteraction, ClientPropertySuggestion, ClientReminder, Deal, Property, Showing


class ClientMatchingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("matching-owner", password="password")
        self.other = User.objects.create_user("matching-other", password="password")
        self.client_record = Client.objects.create(owner=self.owner, name="Анна", phone="+79991110001")
        self.match = Property.objects.create(
            owner=self.owner, title="Квартира в центре", status="available", address="Центр, дом 1",
            deal_type="sale", property_type="apartment", rooms=2, price="5000000", currency="RUB",
            landing_published=True,
        )
        self.expensive = Property.objects.create(
            owner=self.owner, title="Дорогая квартира", status="available", address="Центр, дом 2",
            deal_type="sale", property_type="apartment", rooms=2, price="9000000", currency="RUB",
        )
        self.foreign_property = Property.objects.create(owner=self.other, title="Чужая квартира", status="available")
        self.client.force_login(self.owner)

    def test_search_filters_workspace_inventory_and_keeps_existing_preferences(self):
        response = self.client.post(reverse("client_search_update", args=[self.client_record.pk]), {
            "search_deal_type": "sale", "search_property_type": "apartment", "search_rooms_min": "2",
            "preferred_area": "Центр", "budget": "6000000", "search_currency": "RUB",
            "search_notes": "Тихий двор",
        })
        self.assertEqual(response.status_code, 302)
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.search_notes, "Тихий двор")
        page = self.client.get(reverse("client_detail", args=[self.client_record.pk]))
        self.assertContains(page, "Квартира в центре")
        self.assertEqual(list(page.context["matching_properties"]), [self.match])
        self.assertNotContains(page, "Чужая квартира")

    def test_budget_requires_currency(self):
        self.client.post(reverse("client_search_update", args=[self.client_record.pk]), {"budget": "6000000"})
        self.client_record.refresh_from_db()
        self.assertIsNone(self.client_record.budget)

    def test_commercial_search_matches_area_and_purpose_across_property_types(self):
        office = Property.objects.create(
            owner=self.owner, title="Офис для салона", status="available", property_type="office",
            deal_type="rent", area=Decimal("75.50"), commercial_purpose="Салон красоты",
            price=Decimal("180000"), currency="RUB",
        )
        retail = Property.objects.create(
            owner=self.owner, title="Торговое помещение для салона", status="available", property_type="retail",
            deal_type="rent", area=Decimal("83.25"), commercial_purpose="Салон услуг",
            price=Decimal("190000"), currency="RUB",
        )
        Property.objects.create(
            owner=self.owner, title="Маленький салон", status="available", property_type="commercial",
            deal_type="rent", area=Decimal("45"), commercial_purpose="Салон услуг", price=Decimal("100000"), currency="RUB",
        )
        Property.objects.create(
            owner=self.owner, title="Большой офис", status="available", property_type="office",
            deal_type="rent", area=Decimal("100"), commercial_purpose="Офис продаж", price=Decimal("180000"), currency="RUB",
        )
        response = self.client.post(reverse("client_search_update", args=[self.client_record.pk]), {
            "search_deal_type": "rent", "search_segment": "commercial", "search_property_type": "",
            "search_rooms_min": "5", "search_area_min": "70", "search_area_max": "90",
            "search_commercial_purpose": "салон", "budget": "200000", "search_currency": "RUB",
        })
        self.assertEqual(response.status_code, 302)
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.search_area_min, Decimal("70"))
        self.assertEqual(self.client_record.search_area_max, Decimal("90"))
        self.assertIsNone(self.client_record.search_rooms_min)
        page = self.client.get(reverse("client_detail", args=[self.client_record.pk]))
        self.assertEqual({item.pk for item in page.context["matching_properties"]}, {office.pk, retail.pk})
        self.assertIn("Назначение", page.content.decode())
        self.assertIn("за всё помещение", page.content.decode())

        self.client_record.search_rooms_min = 6  # A legacy preference must not exclude commercial matches.
        self.client_record.save(update_fields=["search_rooms_min"])
        page = self.client.get(reverse("client_detail", args=[self.client_record.pk]))
        self.assertEqual({item.pk for item in page.context["matching_properties"]}, {office.pk, retail.pk})

        office.commercial_purpose = "Офис продаж"
        office.save(update_fields=["commercial_purpose"])
        page = self.client.get(reverse("client_detail", args=[self.client_record.pk]))
        self.assertEqual([item.pk for item in page.context["matching_properties"]], [retail.pk])

    def test_search_rejects_inverted_area_range_and_mismatched_type(self):
        invalid_range = ClientSearchForm({
            "search_segment": "commercial", "search_area_min": "90", "search_area_max": "70",
        }, instance=self.client_record)
        self.assertFalse(invalid_range.is_valid())
        self.assertIn("search_area_max", invalid_range.errors)
        wrong_type = ClientSearchForm({
            "search_segment": "commercial", "search_property_type": "apartment",
        }, instance=self.client_record)
        self.assertFalse(wrong_type.is_valid())
        self.assertIn("search_property_type", wrong_type.errors)

    def test_suggestion_send_and_reaction_are_idempotent_and_recorded(self):
        add = reverse("client_suggestion_create", args=[self.client_record.pk])
        self.client.post(add, {"property": self.match.pk})
        self.client.post(add, {"property": self.match.pk})
        suggestion = ClientPropertySuggestion.objects.get(client=self.client_record)
        self.assertEqual(ClientPropertySuggestion.objects.count(), 1)

        sent = reverse("client_suggestion_sent", args=[self.client_record.pk, suggestion.pk])
        self.client.post(sent)
        self.client.post(sent)
        suggestion.refresh_from_db()
        self.assertIsNotNone(suggestion.sent_at)
        self.assertEqual(ClientInteraction.objects.filter(client=self.client_record, interaction_type="message").count(), 1)

        reaction = reverse("client_suggestion_reaction", args=[self.client_record.pk, suggestion.pk])
        payload = {f"reaction-{suggestion.pk}-reaction": "viewing", f"reaction-{suggestion.pk}-reaction_note": "Удобно в субботу"}
        self.client.post(reaction, payload)
        self.client.post(reaction, payload)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.reaction, "viewing")
        self.assertEqual(suggestion.reaction_note, "Удобно в субботу")
        self.assertIsNotNone(suggestion.reacted_at)
        self.assertEqual(ClientInteraction.objects.filter(client=self.client_record, interaction_type="note").count(), 1)
        page = self.client.get(reverse("client_detail", args=[self.client_record.pk]))
        self.assertContains(page, "Создать сделку и назначить показ")
        self.assertContains(page, reverse("public_landing", args=[self.match.landing_slug]))

    def test_suggestion_row_shows_state_and_keeps_send_and_reaction_collapsed(self):
        self.client.post(reverse("client_suggestion_create", args=[self.client_record.pk]), {"property": self.match.pk})
        suggestion = ClientPropertySuggestion.objects.get(client=self.client_record)
        page = self.client.get(reverse("client_detail", args=[self.client_record.pk]))

        self.assertContains(page, "Не передано")
        self.assertContains(page, f'class="client-matching__share-menu" name="suggestion-{suggestion.pk}"')
        self.assertContains(page, f'class="client-matching__reaction-editor" name="suggestion-{suggestion.pk}"')
        self.assertNotContains(page, f'name="suggestion-{suggestion.pk}" open')
        self.assertContains(page, "Отметить передачу")
        self.assertContains(page, "Изменить реакцию")

        self.client.post(reverse("client_suggestion_sent", args=[self.client_record.pk, suggestion.pk]))
        page = self.client.get(reverse("client_detail", args=[self.client_record.pk]))
        self.assertContains(page, "Передано ·")
        self.assertNotContains(page, "Отметить передачу")

    def test_private_workspace_boundaries(self):
        add = reverse("client_suggestion_create", args=[self.client_record.pk])
        self.client.post(add, {"property": self.foreign_property.pk})
        self.assertFalse(ClientPropertySuggestion.objects.exists())
        foreign_client = Client.objects.create(owner=self.other, name="Чужой клиент", phone="+79991110002")
        self.assertEqual(self.client.post(reverse("client_search_update", args=[foreign_client.pk]), {}).status_code, 404)
        self.assertEqual(self.client.post(reverse("client_suggestion_create", args=[foreign_client.pk]), {"property": self.match.pk}).status_code, 404)
        with self.assertRaises(ValidationError):
            ClientPropertySuggestion.objects.create(client=self.client_record, property=self.foreign_property)

    def test_team_member_can_work_with_shared_client_and_object(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="agent")
        self.client_record.agency = agency
        self.client_record.save()
        self.match.agency = agency
        self.match.save()
        self.client.force_login(self.other)
        response = self.client.post(
            reverse("client_suggestion_create", args=[self.client_record.pk]), {"property": self.match.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ClientPropertySuggestion.objects.get().added_by, self.other)


class ShowingFollowUpTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("showing-followup-owner", password="password")
        self.client_record = Client.objects.create(owner=self.owner, name="Анна", phone="+79991110003")
        self.property = Property.objects.create(owner=self.owner, title="Квартира")
        self.deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property)
        self.start = timezone.localtime(timezone.now() + timedelta(days=2)).replace(second=0, microsecond=0)
        self.showing = Showing.objects.create(owner=self.owner, deal=self.deal, starts_at=self.start)
        self.client.force_login(self.owner)

    def payload(self, **changes):
        return {
            "deal": self.deal.pk,
            "starts_at": self.start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": "", "location": "", "status": "completed", "outcome_note": "Понравилось",
            "next_action": "Позвонить о решении",
            "next_action_due_at": (self.start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            **changes,
        }

    def test_completion_requires_next_step_and_creates_one_visible_task(self):
        url = reverse("showing_detail", args=[self.showing.pk])
        invalid = self.client.post(url, self.payload(next_action="", next_action_due_at=""))
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Укажите, что сделать после показа")
        self.showing.refresh_from_db()
        self.assertEqual(self.showing.status, "planned")
        self.assertFalse(ClientReminder.objects.exists())

        self.assertEqual(self.client.post(url, self.payload()).status_code, 302)
        self.showing.refresh_from_db()
        task = self.showing.follow_up_task
        self.assertEqual(task.client, self.client_record)
        self.assertEqual(task.deal, self.deal)
        self.assertEqual(task.owner, self.owner)
        self.assertContains(self.client.get(reverse("work_items")), "Позвонить о решении")

        self.client.post(url, self.payload())
        self.assertEqual(ClientReminder.objects.filter(deal=self.deal).count(), 1)
        self.client.post(url, self.payload(status="cancelled", outcome_note="", next_action="", next_action_due_at=""))
        self.showing.refresh_from_db()
        task.refresh_from_db()
        self.assertIsNone(self.showing.follow_up_task_id)
        self.assertIsNotNone(task.cancelled_at)
        self.assertNotContains(self.client.get(reverse("work_items")), "Позвонить о решении")

    def test_team_follow_up_is_assigned_to_deal_responsible(self):
        agent = User.objects.create_user("showing-followup-agent", password="password")
        agency = Agency.objects.create(owner=self.owner, name="Команда показов")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=agent, role="agent")
        self.client_record.agency = agency
        self.client_record.save()
        self.property.agency = agency
        self.property.save()
        self.deal.agency = agency
        self.deal.responsible = agent
        self.deal.save()
        self.showing.agency = agency
        self.showing.save()

        response = self.client.post(reverse("showing_detail", args=[self.showing.pk]), self.payload())
        self.assertEqual(response.status_code, 302)
        self.showing.refresh_from_db()
        self.assertEqual(self.showing.follow_up_task.owner, agent)
        self.client.force_login(agent)
        self.assertContains(self.client.get(reverse("work_items")), "Позвонить о решении")

    def test_viewing_cannot_be_recorded_without_showing_workflow(self):
        self.client.post(reverse("client_interaction_create", args=[self.client_record.pk]), {
            "interaction_type": "viewing", "text": "Осмотрели квартиру",
        })
        self.assertFalse(self.client_record.interactions.exists())
